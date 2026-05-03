package main

import (
	"encoding/xml"
	"flag"
	"fmt"
	"net"
	"net/http"
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
	mux.HandleFunc("/dial/dd.xml", handleDIALDesc)
	mux.HandleFunc("/apps/YouTube/run", handleYouTubeInstance)
	mux.HandleFunc("/apps/YouTube", handleYouTubeApp)
	mux.HandleFunc("/apps/", handleApps)

	go func() {
		srv := &http.Server{
			Addr:              fmt.Sprintf(":%d", dialHTTPPort),
			Handler:           mux,
			ReadHeaderTimeout: 5 * time.Second,
		}
		if err := srv.ListenAndServe(); err != nil {
			msgPrintln(fmt.Sprintf("error DIAL HTTP: %v", err))
		}
	}()

	go runSSDPListener()
}

func handleDIALDesc(w http.ResponseWriter, r *http.Request) {
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
	switch r.Method {
	case http.MethodGet:
		dialStateMu.RLock()
		state := "stopped"
		if curVideoId != "" {
			state = "running"
		}
		dialStateMu.RUnlock()

		w.Header().Set("Content-Type", "application/xml; charset=utf-8")
		w.Header().Set("Access-Control-Allow-Origin", "https://www.youtube.com")
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
		w.Header().Set("Location", fmt.Sprintf("http://%s:%d/apps/YouTube/run", dialLocalIP, dialHTTPPort))
		w.WriteHeader(http.StatusCreated)
	case http.MethodDelete:
		msgPrintln("stop")
		w.WriteHeader(http.StatusOK)
	default:
		w.Header().Set("Allow", "GET, POST, DELETE")
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func handleYouTubeInstance(w http.ResponseWriter, r *http.Request) {
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
	}
	defer conn.Close()
	conn.SetReadBuffer(4096)

	buf := make([]byte, 2048)
	for {
		n, from, err := conn.ReadFromUDP(buf)
		if err != nil {
			continue
		}
		if isSSDPSearch(string(buf[:n])) {
			// Reply from the listening socket so the source port is 1900,
			// which some DIAL clients require.
			if err := sendSSDPResponse(conn, from); err != nil {
				dbgPrintln(fmt.Sprintf("error SSDP response: %v", err))
			}
		}
	}
}

func isSSDPSearch(msg string) bool {
	return strings.Contains(msg, "M-SEARCH") &&
		(strings.Contains(msg, dialSvcType) || strings.Contains(msg, "ssdp:all"))
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
	_, err := conn.WriteToUDP([]byte(resp), to)
	return err
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
