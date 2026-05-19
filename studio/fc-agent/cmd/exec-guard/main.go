// studio-exec-guard: Content-hash exec verification wrapper.
// Invoked by bubblewrap to intercept exec calls inside the Firecracker guest.
// Reads STUDIO_EXEC_MANIFEST env (JSON mapping binary_path -> SHA256),
// hashes the target binary, and only allows execution if the hash matches.

package main

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"syscall"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "studio-exec-guard: missing target binary")
		os.Exit(126)
	}

	targetBinary := os.Args[1]

	manifestJSON := os.Getenv("STUDIO_EXEC_MANIFEST")
	if manifestJSON == "" {
		fmt.Fprintln(os.Stderr, "studio-exec-guard: STUDIO_EXEC_MANIFEST not set — denying exec")
		os.Exit(126)
	}

	var manifest map[string]string
	if err := json.Unmarshal([]byte(manifestJSON), &manifest); err != nil {
		fmt.Fprintf(os.Stderr, "studio-exec-guard: invalid STUDIO_EXEC_MANIFEST: %v\n", err)
		os.Exit(126)
	}

	expectedHash, ok := manifest[targetBinary]
	if !ok {
		fmt.Fprintf(os.Stderr, "studio-exec-guard: binary %q not in exec allowlist — denying exec\n", targetBinary)
		os.Exit(126)
	}

	actualHash, err := hashFile(targetBinary)
	if err != nil {
		fmt.Fprintf(os.Stderr, "studio-exec-guard: cannot hash %q: %v\n", targetBinary, err)
		os.Exit(126)
	}

	if actualHash != expectedHash {
		fmt.Fprintf(os.Stderr, "studio-exec-guard: hash mismatch for %q — denying exec\n", targetBinary)
		fmt.Fprintf(os.Stderr, "  expected: %s\n  actual:   %s\n", expectedHash[:16], actualHash[:16])
		// Write audit event via RPC to orchestrator (best-effort)
		sendAuditEvent(targetBinary, expectedHash, actualHash)
		os.Exit(126)
	}

	// Hash matches — exec the real binary with remaining args
	binaryPath, err := exec.LookPath(targetBinary)
	if err != nil {
		fmt.Fprintf(os.Stderr, "studio-exec-guard: %q not found: %v\n", targetBinary, err)
		os.Exit(126)
	}

	argv := append([]string{targetBinary}, os.Args[2:]...)
	if err := syscall.Exec(binaryPath, argv, os.Environ()); err != nil {
		fmt.Fprintf(os.Stderr, "studio-exec-guard: exec %q: %v\n", targetBinary, err)
		os.Exit(126)
	}
}

func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()

	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", h.Sum(nil)), nil
}

func sendAuditEvent(binaryPath, expectedHash, actualHash string) {
	// Best-effort audit event to the orchestrator via a file that init picks up.
	// The guest init script tails this file and forwards to the orchestrator.
	auditLog := "/run/studio/audit-events.jsonl"
	f, err := os.OpenFile(auditLog, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		return
	}
	defer f.Close()

	event := map[string]any{
		"event":         "exec_hash_mismatch",
		"binary":        binaryPath,
		"expected_hash": expectedHash,
		"actual_hash":   actualHash,
	}
	data, _ := json.Marshal(event)
	f.Write(append(data, '\n'))
}
