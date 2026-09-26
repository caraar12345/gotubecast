package main

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net/http"
	"os"
)

// outboundHTTP is used for all outbound HTTPS to YouTube. It uses the default
// transport (including HTTPS_PROXY from the environment) unless -debug-root-ca
// is set, in which case the transport is cloned with an augmented root CA pool.
var outboundHTTP = http.DefaultClient

func initOutboundHTTP(extraRootCAPath string) error {
	if extraRootCAPath == "" {
		return nil
	}
	pemData, err := os.ReadFile(extraRootCAPath)
	if err != nil {
		return fmt.Errorf("debug-root-ca: read file: %w", err)
	}
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil {
		pool = x509.NewCertPool()
	}
	if ok := pool.AppendCertsFromPEM(pemData); !ok {
		return fmt.Errorf("debug-root-ca: no PEM certificates found in %q", extraRootCAPath)
	}
	tr, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return fmt.Errorf("http.DefaultTransport is not *http.Transport")
	}
	t := tr.Clone()
	if t.TLSClientConfig == nil {
		t.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	} else {
		t.TLSClientConfig = t.TLSClientConfig.Clone()
		t.TLSClientConfig.MinVersion = tls.VersionTLS12
	}
	t.TLSClientConfig.RootCAs = pool
	outboundHTTP = &http.Client{Transport: t}
	return nil
}
