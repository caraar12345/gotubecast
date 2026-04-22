package main

import (
	"flag"
	"fmt"
	"net"
	"net/http"
	"strings"
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
)

func init() {
	flag.IntVar(&dialHTTPPort, "p", 8008, "DIAL server HTTP port (0 to disable)")
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
		if err := http.ListenAndServe(fmt.Sprintf(":%d", dialHTTPPort), mux); err != nil {
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
</root>`, screenName, screenUid)
}

func handleYouTubeApp(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		state := "stopped"
		if curVideoId != "" {
			state = "running"
		}
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
</service>`, state, screenId, currentLoungeToken, screenUid, screenName)
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
			go sendSSDPResponse(from)
		}
	}
}

func isSSDPSearch(msg string) bool {
	return strings.Contains(msg, "M-SEARCH") &&
		(strings.Contains(msg, dialSvcType) || strings.Contains(msg, "ssdp:all"))
}

func sendSSDPResponse(to *net.UDPAddr) {
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
	conn, err := net.DialUDP("udp4", nil, to)
	if err != nil {
		return
	}
	defer conn.Close()
	conn.Write([]byte(resp))
}

// getOutboundIP returns the host's preferred outbound IP by probing a UDP connection.
func getOutboundIP() string {
	conn, err := net.Dial("udp4", "8.8.8.8:80")
	if err != nil {
		return "127.0.0.1"
	}
	defer conn.Close()
	return conn.LocalAddr().(*net.UDPAddr).IP.String()
}
