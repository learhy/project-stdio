.PHONY: build build-static clean

GO ?= go
OUTPUT ?= studio-fc-agent

build:
	$(GO) build -ldflags="-s -w" -o $(OUTPUT) .

build-static:
	CGO_ENABLED=0 GOOS=linux $(GO) build -ldflags="-s -w" -o $(OUTPUT) .

clean:
	rm -f $(OUTPUT)
