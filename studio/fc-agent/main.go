// studio-fc-agent: Minimal guest agent for Firecracker microVMs.
// Listens on vsock port 52 for JSON commands from the host orchestrator.
// Protocol: 4-byte big-endian length prefix + JSON payload, both directions.

package main

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"sync"
	"syscall"

	"github.com/mdlayher/vsock"
)

const (
	agentPort   = 52
	resetSignal = syscall.SIGUSR1
)

// ── Protocol messages ──────────────────────────────────────────────────────────

type BwrapConfig struct {
	UseBwrap  bool     `json:"use_bwrap"`
	BwrapArgs []string `json:"bwrap_args"`
}

type Request struct {
	Cmd    string            `json:"cmd"`
	Argv  []string          `json:"argv,omitempty"`
	Env   map[string]string `json:"env,omitempty"`
	PID   int               `json:"pid,omitempty"`
	Signal string           `json:"signal,omitempty"`
	Bwrap  *BwrapConfig     `json:"bwrap,omitempty"`
}

type Response struct {
	OK      bool              `json:"ok"`
	PID     int               `json:"pid,omitempty"`
	Running bool              `json:"running,omitempty"`
	Error   string            `json:"error,omitempty"`
	Data    map[string]any   `json:"data,omitempty"`
}

// ── Process tracking ───────────────────────────────────────────────────────────

type trackedProcess struct {
	cmd    *exec.Cmd
	done   chan struct{}
	exitCode int
}

var (
	processes   = map[int]*trackedProcess{}
	processesMu sync.Mutex
	nextPID     = 1000
)

func trackProcess(cmd *exec.Cmd) int {
	processesMu.Lock()
	pid := nextPID
	nextPID++
	tp := &trackedProcess{cmd: cmd, done: make(chan struct{}), exitCode: -1}
	processes[pid] = tp
	processesMu.Unlock()

	go func() {
		err := cmd.Wait()
		tp.exitCode = 0
		if err != nil {
			if exiterr, ok := err.(*exec.ExitError); ok {
				tp.exitCode = exiterr.ExitCode()
			} else {
				tp.exitCode = -1
			}
		}
		close(tp.done)
		processesMu.Lock()
		delete(processes, pid)
		processesMu.Unlock()
	}()

	return pid
}

// ── Command handlers ───────────────────────────────────────────────────────────

func handleExec(req Request) Response {
	var cmd *exec.Cmd
	if req.Bwrap != nil && req.Bwrap.UseBwrap && len(req.Bwrap.BwrapArgs) > 0 {
		// Wrap command in bubblewrap for exec allowlist enforcement
		fullArgv := append(req.Bwrap.BwrapArgs, "--")
		fullArgv = append(fullArgv, req.Argv...)
		cmd = exec.Command("bwrap", fullArgv...)
	} else {
		cmd = exec.Command(req.Argv[0], req.Argv[1:]...)
	}
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Stdin = os.Stdin

	if len(req.Env) > 0 {
		cmd.Env = os.Environ()
		for k, v := range req.Env {
			cmd.Env = append(cmd.Env, fmt.Sprintf("%s=%s", k, v))
		}
	}

	if err := cmd.Start(); err != nil {
		return Response{OK: false, Error: err.Error()}
	}

	pid := trackProcess(cmd)
	return Response{OK: true, PID: pid}
}

func handleSignal(req Request) Response {
	processesMu.Lock()
	tp, ok := processes[req.PID]
	processesMu.Unlock()
	if !ok {
		return Response{OK: false, Error: fmt.Sprintf("pid %d not found", req.PID)}
	}

	sig := syscall.SIGTERM
	if req.Signal == "KILL" {
		sig = syscall.SIGKILL
	} else if req.Signal == "INT" {
		sig = syscall.SIGINT
	} else if req.Signal == "HUP" {
		sig = syscall.SIGHUP
	}

	if err := tp.cmd.Process.Signal(sig); err != nil {
		return Response{OK: false, Error: err.Error()}
	}
	return Response{OK: true}
}

func handleIsRunning(req Request) Response {
	processesMu.Lock()
	_, ok := processes[req.PID]
	processesMu.Unlock()
	return Response{OK: true, Running: ok}
}

func handleReset() Response {
	// Signal init to reset overlay
	proc, _ := os.FindProcess(1)
	if proc != nil {
		proc.Signal(resetSignal)
	}
	return Response{OK: true}
}

// ── Main loop ──────────────────────────────────────────────────────────────────

func main() {
	log.SetPrefix("[fc-agent] ")
	log.SetFlags(log.Ltime | log.Lmicroseconds)

	listener, err := vsock.Listen(agentPort, nil)
	if err != nil {
		log.Fatalf("vsock listen on port %d: %v", agentPort, err)
	}
	defer listener.Close()
	log.Printf("listening on vsock port %d", agentPort)

	for {
		conn, err := listener.Accept()
		if err != nil {
			log.Printf("accept error: %v", err)
			continue
		}
		go handleConn(conn)
	}
}

func handleConn(conn net.Conn) {
	defer conn.Close()

	lenBuf := make([]byte, 4)
	for {
		// Read 4-byte length prefix
		if _, err := io.ReadFull(conn, lenBuf); err != nil {
			if err != io.EOF {
				log.Printf("read length: %v", err)
			}
			return
		}
		payloadLen := binary.BigEndian.Uint32(lenBuf)
		if payloadLen > 1<<20 { // 1MB sanity cap
			log.Printf("payload too large: %d", payloadLen)
			return
		}
		payload := make([]byte, payloadLen)
		if _, err := io.ReadFull(conn, payload); err != nil {
			log.Printf("read payload: %v", err)
			return
		}

		var req Request
		if err := json.Unmarshal(payload, &req); err != nil {
			sendResponse(conn, Response{OK: false, Error: fmt.Sprintf("json decode: %v", err)})
			continue
		}

		var resp Response
		switch req.Cmd {
		case "exec":
			resp = handleExec(req)
		case "signal":
			resp = handleSignal(req)
		case "is_running":
			resp = handleIsRunning(req)
		case "reset":
			resp = handleReset()
		case "ping":
			resp = Response{OK: true}
		default:
			resp = Response{OK: false, Error: fmt.Sprintf("unknown command: %s", req.Cmd)}
		}

		sendResponse(conn, resp)
	}
}

func sendResponse(conn net.Conn, resp Response) {
	data, _ := json.Marshal(resp)
	lenBuf := make([]byte, 4)
	binary.BigEndian.PutUint32(lenBuf, uint32(len(data)))
	conn.Write(lenBuf)
	conn.Write(data)
}

