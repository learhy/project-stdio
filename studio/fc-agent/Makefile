.PHONY: build build-static clean build-exec-guard

GO ?= go
OUTPUT ?= studio-fc-agent
EXEC_GUARD_OUTPUT ?= studio-exec-guard

build: build-exec-guard
	$(GO) build -ldflags="-s -w" -o $(OUTPUT) .

build-static: build-exec-guard
	CGO_ENABLED=0 GOOS=linux $(GO) build -ldflags="-s -w" -o $(OUTPUT) .

build-exec-guard:
	CGO_ENABLED=0 $(GO) build -ldflags="-s -w" -o $(EXEC_GUARD_OUTPUT) ./cmd/exec-guard/

clean:
	rm -f $(OUTPUT) $(EXEC_GUARD_OUTPUT)
