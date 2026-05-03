package main

import (
	"encoding/xml"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

const (
	ssdpMulticast = "239.255.255.250"
	ssdpPort      = 1900
	dialSvcType   = "urn:dial-multiscreen-org:service:dial:1"
)

var (
	dialHTTPPort       int
	currentLoungeToken string
	dialLocalIP        string

	// dialStateMu guards curVideoId reads in HTTP handler goroutines against
	// concurrent writes from the Lounge command loop in main.go.
	dialStateMu sync.RWMutex
)

func init() {
	flag.IntVar(&dialHTTPPort, "p", 8008, "DIAL server HTTP port (0 to disable)")
}

// xmlEsc returns s with XML special characters escaped.
func xmlEsc(s string) string {
	var b strings.Builder
	xml.EscapeText(&b, []byte(s))
	return b.String()
}

// startDIAL starts the DIAL HTTP server and SSDP multicast listener.
// Must be called after screenId and currentLoungeToken are set.
func startDIAL() {
	if dialHTTPPort == 0 {
		return
	}
	dialLocalIP = getOutboundIP()
	msgPrintln(fmt.Sprintf("dial_url http://%s:%d/apps/YouTube", dialLocalIP, dialHTTPPort))

	mux := http.NewServeMux()
	mux.HandleFunc("/dd.xml", handleDIALDesc)
	mux.HandleFunc("/dial/dd.xml", handleDIALDesc)
	mux.HandleFunc("/ssdp/device-desc.xml", handleDIALDesc)
	mux.HandleFunc("/apps/YouTube/run", handleYouTubeInstance)
	mux.HandleFunc("/apps/YouTube", handleYouTubeApp)
	mux.HandleFunc("/apps/", handleApps)

	go func() {
		srv := &http.Server{
			Addr:              fmt.Sprintf(":%d", dialHTTPPort),
			Handler:           mux,
			ReadHeaderTimeout: 5 * time.Second,
		}
		dialTracef("http_listen addr=%s local_ip=%s", srv.Addr, dialLocalIP)
		if err := srv.ListenAndServe(); err != nil {
			msgPrintln(fmt.Sprintf("error DIAL HTTP: %v", err))
		}
	}()

	go runSSDPListener()
}

func handleDIALDesc(w http.ResponseWriter, r *http.Request) {
	dialTracef("http_req method=%s path=%s remote=%s ua=%q", r.Method, r.URL.Path, r.RemoteAddr, r.UserAgent())
	w.Header().Set("Content-Type", "application/xml; charset=utf-8")
	w.Header().Set("Application-URL", fmt.Sprintf("http://%s:%d/apps/", dialLocalIP, dialHTTPPort))
	fmt.Fprintf(w, `<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:dial-multiscreen-org:device:dial:1</deviceType>
    <friendlyName>%s</friendlyName>
    <manufacturer>GoTubeCast</manufacturer>
    <modelName>GoTubeCast</modelName>
    <UDN>uuid:%s</UDN>
  </device>
</root>`, xmlEsc(screenName), xmlEsc(screenUid))
}

func handleYouTubeApp(w http.ResponseWriter, r *http.Request) {
	dialTracef("http_req method=%s path=%s remote=%s ua=%q query=%q", r.Method, r.URL.Path, r.RemoteAddr, r.UserAgent(), r.URL.RawQuery)
	w.Header().Set("Access-Control-Allow-Origin", "https://www.youtube.com")
	w.Header().Set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Origin, Accept")
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return
	}

	switch r.Method {
	case http.MethodGet:
		dialStateMu.RLock()
		state := "stopped"
		if curVideoId != "" {
			state = "running"
		}
		dialStateMu.RUnlock()

		w.Header().Set("Content-Type", "application/xml; charset=utf-8")
		fmt.Fprintf(w, `<?xml version="1.0" encoding="UTF-8"?>
<service xmlns="urn:dial-multiscreen-org:schemas:dial">
  <name>YouTube</name>
  <options allowStop="true"/>
  <state>%s</state>
  <additionalData>
    <screenId>%s</screenId>
    <loungeToken>%s</loungeToken>
    <mdxVersion>2</mdxVersion>
    <deviceId>%s</deviceId>
    <deviceName>%s</deviceName>
  </additionalData>
</service>`, state, xmlEsc(screenId), xmlEsc(currentLoungeToken), xmlEsc(screenUid), xmlEsc(screenName))
	case http.MethodPost:
		// Standard DIAL uses application/x-www-form-urlencoded. The iOS YouTube
		// app sends the same key=value body as Content-Type: text/plain, which
		// net/http ParseForm does not decode — pairing would never register and
		// get_screen would 404 (seen in Proxygen traces).
		code := ""
		if err := r.ParseForm(); err == nil {
			dialTracef("youtube_post_form %s", formatURLValues(r.Form))
			code = r.FormValue("pairingCode")
		} else {
			dialTracef("youtube_post_parse_form_error %v", err)
		}
		if code == "" {
			b, err := io.ReadAll(io.LimitReader(r.Body, 64<<10))
			if err != nil {
				dialTracef("youtube_post_body_read_error %v", err)
			} else if len(b) > 0 {
				vals, err := url.ParseQuery(strings.TrimSpace(string(b)))
				if err != nil {
					dialTracef("youtube_post_body_parse_query_error %v", err)
				} else {
					dialTracef("youtube_post_body_query %s", formatURLValues(vals))
					code = vals.Get("pairingCode")
				}
			}
		}
		if code != "" {
			// Register before responding: iOS polls get_screen immediately after
			// 201; a background goroutine loses the race and get_screen stays 404
			// until the client gives up (Proxygen).
			registerDialPairingCode(code)
		} else {
			dialTracef("youtube_post_missing_pairing_code")
		}
		w.Header().Set("Location", fmt.Sprintf("http://%s:%d/apps/YouTube/run", dialLocalIP, dialHTTPPort))
		w.WriteHeader(http.StatusCreated)
	case http.MethodDelete:
		msgPrintln("stop")
		w.WriteHeader(http.StatusOK)
	default:
		w.Header().Set("Allow", "GET, POST, DELETE, OPTIONS")
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

// registerDialPairingCode tells YouTube to associate the phone-generated DIAL
// pairing code with our screen's lounge token. Without this the phone's
// follow-up GET /api/lounge/pairing/get_screen?pairing_code=<UUID> returns 404
// and the phone aborts the DIAL launch.
func registerDialPairingCode(code string) {
	dialTracef("pairing_register_start pairing_code=%s", code)
	vals := url.Values{
		"access_type":  {"permanent"},
		"app":          {screenApp},
		"lounge_token": {currentLoungeToken},
		"screen_id":    {screenId},
		"screen_name":  {screenName},
		"pairing_code": {code},
	}
	resp, err := http.PostForm("https://www.youtube.com/api/lounge/pairing/get_pairing_code?ctx=pair", vals)
	if err != nil {
		dbgPrintln(fmt.Sprintf("dial: pairing register error: %v", err))
		return
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	dialTracef("pairing_register_resp status=%d body=%s", resp.StatusCode, sanitizeLogBody(string(body)))
	dbgPrintln(fmt.Sprintf("dial: pairing register: HTTP %d %s", resp.StatusCode, string(body)))
}

func handleYouTubeInstance(w http.ResponseWriter, r *http.Request) {
	dialTracef("http_req method=%s path=%s remote=%s ua=%q", r.Method, r.URL.Path, r.RemoteAddr, r.UserAgent())
	if r.Method == http.MethodDelete {
		msgPrintln("stop")
		w.WriteHeader(http.StatusOK)
		return
	}
	w.Header().Set("Content-Type", "application/xml; charset=utf-8")
	fmt.Fprint(w, `<?xml version="1.0" encoding="UTF-8"?>
<service xmlns="urn:dial-multiscreen-org:schemas:dial">
  <name>YouTube</name>
  <options allowStop="true"/>
  <state>running</state>
</service>`)
}

func handleApps(w http.ResponseWriter, r *http.Request) {
	dialTracef("http_req method=%s path=%s remote=%s ua=%q status=404", r.Method, r.URL.Path, r.RemoteAddr, r.UserAgent())
	w.WriteHeader(http.StatusNotFound)
}

// runSSDPListener joins the DIAL multicast group and replies to M-SEARCH probes.
func runSSDPListener() {
	groupAddr := &net.UDPAddr{
		IP:   net.ParseIP(ssdpMulticast),
		Port: ssdpPort,
	}
	conn, err := net.ListenMulticastUDP("udp4", nil, groupAddr)
	if err != nil {
		// Multicast may be unavailable (e.g. missing permission); try plain UDP.
		msgPrintln(fmt.Sprintf("error SSDP multicast: %v — retrying on 0.0.0.0:1900", err))
		conn2, err2 := net.ListenUDP("udp4", &net.UDPAddr{Port: ssdpPort})
		if err2 != nil {
			msgPrintln(fmt.Sprintf("error SSDP: %v", err2))
			return
		}
		conn = conn2
		dialTracef("ssdp_mode unicast_fallback listen=0.0.0.0:%d", ssdpPort)
	} else {
		dialTracef("ssdp_mode multicast listen=%s:%d", ssdpMulticast, ssdpPort)
	}
	defer conn.Close()
	conn.SetReadBuffer(4096)

	buf := make([]byte, 2048)
	for {
		n, from, err := conn.ReadFromUDP(buf)
		if err != nil {
			continue
		}
		msg := string(buf[:n])
		if traceProtocol {
			dialTracef("ssdp_rx from=%s size=%d first_line=%q", from, n, firstLine(msg))
		}
		if isSSDPSearch(msg) {
			// Reply from the listening socket so the source port is 1900,
			// which some DIAL clients require.
			if err := sendSSDPResponse(conn, from); err != nil {
				dbgPrintln(fmt.Sprintf("error SSDP response: %v", err))
			}
		}
	}
}

func isSSDPSearch(msg string) bool {
	upper := strings.ToUpper(msg)
	stNeedle := strings.ToUpper(dialSvcType)
	return strings.Contains(upper, "M-SEARCH") &&
		(strings.Contains(upper, stNeedle) || strings.Contains(upper, "SSDP:ALL"))
}

func sendSSDPResponse(conn *net.UDPConn, to *net.UDPAddr) error {
	resp := fmt.Sprintf(
		"HTTP/1.1 200 OK\r\n"+
			"CACHE-CONTROL: max-age=1800\r\n"+
			"EXT:\r\n"+
			"LOCATION: http://%s:%d/dial/dd.xml\r\n"+
			"SERVER: Linux/1.0 UPnP/1.1 GoTubeCast/1.0\r\n"+
			"ST: %s\r\n"+
			"USN: uuid:%s::%s\r\n"+
			"Application-URL: http://%s:%d/apps/\r\n\r\n",
		dialLocalIP, dialHTTPPort,
		dialSvcType,
		screenUid, dialSvcType,
		dialLocalIP, dialHTTPPort,
	)
	dialTracef("ssdp_tx to=%s location=http://%s:%d/dial/dd.xml", to, dialLocalIP, dialHTTPPort)
	_, err := conn.WriteToUDP([]byte(resp), to)
	return err
}

func dialTracef(format string, args ...interface{}) {
	if !traceProtocol {
		return
	}
	dbgPrintln(fmt.Sprintf("dial_trace "+format, args...))
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return strings.TrimSpace(s[:i])
	}
	return strings.TrimSpace(s)
}

// getOutboundIP returns the first non-loopback IPv4 address found on an active
// interface. Falls back to 127.0.0.1 if none is found.
func getOutboundIP() string {
	ifaces, err := net.Interfaces()
	if err != nil {
		return "127.0.0.1"
	}
	for _, iface := range ifaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			ipNet, ok := addr.(*net.IPNet)
			if !ok {
				continue
			}
			if ip := ipNet.IP.To4(); ip != nil {
				return ip.String()
			}
		}
	}
	return "127.0.0.1"
}
