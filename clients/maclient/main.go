// maclient — MiniAssistant Go-Client
//
// Nutzung:
//   maclient                   – Chat starten (liest Config)
//   maclient config            – Konfiguration erstellen/bearbeiten
//   maclient config --show     – aktuelle Config anzeigen
//   maclient --sessions        – alle Sessions anzeigen
//   maclient --continue [N]    – bestimmte Session fortsetzen
//
// Config-Datei: ~/.config/miniassistant/config.json
// Benötigt: keine externen Abhängigkeiten (nur Go-Stdlib)

package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/ergochat/readline"
)

// ---------------------------------------------------------------------------
// Konfiguration
// ---------------------------------------------------------------------------

type Config struct {
	Server     string   `json:"server"`
	Token      string   `json:"token,omitempty"`
	LocalTools []string `json:"local_tools"`
	Model      string   `json:"model,omitempty"`
	Proxy      string   `json:"proxy,omitempty"` // http://, https://, socks5://
}

var configPath string
var sessionsDir string

func init() {
	home, _ := os.UserHomeDir()
	dir := filepath.Join(home, ".config", "miniassistant")
	configPath = filepath.Join(dir, "config.json")
	sessionsDir = filepath.Join(dir, "sessions")
}

// ---------------------------------------------------------------------------
// Session-Persistenz (Multi-Session)
// ---------------------------------------------------------------------------

type Session struct {
	ID        string           `json:"id"`
	Messages  []map[string]any `json:"messages,omitempty"`
	UpdatedAt string           `json:"updated_at"`
	Preview   string           `json:"preview,omitempty"`
}

func sessionFilePath(id string) string {
	return filepath.Join(sessionsDir, id+".json")
}

func saveSession(s Session) {
	os.MkdirAll(sessionsDir, 0700)
	path := sessionFilePath(s.ID)
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0600); err != nil {
		return
	}
	os.Rename(tmp, path)
}

func listSessions() []Session {
	entries, err := os.ReadDir(sessionsDir)
	if err != nil {
		return nil
	}
	var sessions []Session
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(sessionsDir, e.Name()))
		if err != nil {
			continue
		}
		var s Session
		if json.Unmarshal(data, &s) == nil && s.ID != "" {
			sessions = append(sessions, s)
		}
	}
	sort.Slice(sessions, func(i, j int) bool {
		return sessions[i].UpdatedAt > sessions[j].UpdatedAt
	})
	return sessions
}

func findSession(query string, sessions []Session) *Session {
	if n, err := strconv.Atoi(query); err == nil {
		if n >= 1 && n <= len(sessions) {
			s := sessions[n-1]
			return &s
		}
		return nil
	}
	for i := range sessions {
		if strings.HasPrefix(sessions[i].ID, query) {
			return &sessions[i]
		}
	}
	return nil
}

func showSessions(sessions []Session) {
	if len(sessions) == 0 {
		fmt.Printf("%sKeine gespeicherten Sessions.%s\n", dim, reset)
		return
	}
	fmt.Printf("%sSessions:%s\n", bold, reset)
	for i, s := range sessions {
		updatedAt := ""
		if s.UpdatedAt != "" {
			if t, err := time.Parse(time.RFC3339, s.UpdatedAt); err == nil {
				updatedAt = t.Format("02.01. 15:04")
			}
		}
		idShort := s.ID
		if len(idShort) > 8 {
			idShort = idShort[:8]
		}
		preview := s.Preview
		if preview == "" {
			preview = "(leer)"
		}
		fmt.Printf("  %s%2d%s  %s%-12s%s  %s%s…%s  %s\n",
			bold, i+1, reset,
			dim, updatedAt, reset,
			dim, idShort, reset,
			preview)
	}
}

// migrateOldSession verschiebt eine einzelne session.json ins sessions/-Verzeichnis.
func migrateOldSession() {
	home, _ := os.UserHomeDir()
	oldPath := filepath.Join(home, ".config", "miniassistant", "session.json")
	data, err := os.ReadFile(oldPath)
	if err != nil {
		return
	}
	var s Session
	if json.Unmarshal(data, &s) == nil && s.ID != "" {
		saveSession(s)
	}
	os.Remove(oldPath)
}

// fetchTitle ruft /api/title auf und generiert einen kurzen Titel.
func fetchTitle(cfg Config, firstMessage string) string {
	payload := map[string]any{"message": firstMessage}
	body, err := json.Marshal(payload)
	if err != nil {
		return ""
	}
	req, err := http.NewRequest("POST", cfg.Server+"/api/title", bytes.NewReader(body))
	if err != nil {
		return ""
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+cfg.Token)
	c := &http.Client{Timeout: 30 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	var result struct {
		Title string `json:"title"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return ""
	}
	return strings.TrimSpace(result.Title)
}

func sessionPreview(messages []map[string]any) string {
	for _, m := range messages {
		if role, _ := m["role"].(string); role == "user" {
			if content, _ := m["content"].(string); content != "" {
				if len([]rune(content)) > 70 {
					return string([]rune(content)[:70]) + "…"
				}
				return content
			}
		}
	}
	return ""
}

func loadConfig() (Config, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return Config{LocalTools: []string{}}, err
	}
	var c Config
	if err := json.Unmarshal(data, &c); err != nil {
		return Config{LocalTools: []string{}}, err
	}
	if c.LocalTools == nil {
		c.LocalTools = []string{}
	}
	return c, nil
}

func saveConfig(c Config) error {
	if err := os.MkdirAll(filepath.Dir(configPath), 0700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	tmp := configPath + ".tmp"
	if err := os.WriteFile(tmp, data, 0600); err != nil {
		return err
	}
	return os.Rename(tmp, configPath)
}

// ---------------------------------------------------------------------------
// ANSI-Farben (nur auf echtem Terminal)
// ---------------------------------------------------------------------------

var (
	bold   = ""
	green  = ""
	dim    = ""
	reset  = ""
	yellow = ""
	cyan   = ""
)

func getTermWidth() int {
	if col := os.Getenv("COLUMNS"); col != "" {
		if n, err := strconv.Atoi(col); err == nil && n > 20 {
			return n
		}
	}
	return 80
}

func initColors() {
	if fi, err := os.Stdout.Stat(); err == nil && (fi.Mode()&os.ModeCharDevice) != 0 {
		bold   = "\033[1m"
		green  = "\033[32m"
		dim    = "\033[2m"
		reset  = "\033[0m"
		yellow = "\033[33m"
		cyan   = "\033[36m"
	}
}

// ---------------------------------------------------------------------------
// Konfigurationsassistent
// ---------------------------------------------------------------------------

var availableLocalTools = []struct {
	Name        string
	Description string
}{
	{"exec",     "Shell-Befehle ausführen"},
	{"read_url", "URLs abrufen"},
}

func runConfig(showOnly bool) {
	cfg, _ := loadConfig()

	if showOnly {
		fmt.Printf("%sMiniAssistant Konfiguration%s\n", bold, reset)
		fmt.Printf("  Datei:       %s\n", configPath)
		fmt.Printf("  Server:      %s%s%s\n", cyan, cfg.Server, reset)
		if cfg.Token != "" {
			fmt.Printf("  Token:       %s[gesetzt]%s\n", dim, reset)
		} else {
			fmt.Printf("  Token:       %s(kein Token)%s\n", yellow, reset)
		}
		if cfg.Model != "" {
			fmt.Printf("  Modell:      %s\n", cfg.Model)
		} else {
			fmt.Printf("  Modell:      %s(Serverstandard)%s\n", dim, reset)
		}
		if len(cfg.LocalTools) > 0 {
			fmt.Printf("  Lok. Tools:  %s\n", strings.Join(cfg.LocalTools, ", "))
		} else {
			fmt.Printf("  Lok. Tools:  %s(alle serverseitig)%s\n", dim, reset)
		}
		if cfg.Proxy != "" {
			fmt.Printf("  Proxy:       %s%s%s\n", cyan, cfg.Proxy, reset)
		} else {
			fmt.Printf("  Proxy:       %s(kein Proxy)%s\n", dim, reset)
		}
		return
	}

	fmt.Printf("%sMiniAssistant — Konfiguration%s\n\n", bold, reset)
	fmt.Printf("%sKonfigurationsdatei:%s %s\n\n", dim, reset, configPath)

	r := bufio.NewReader(os.Stdin)

	// --- Server URL ---
	fmt.Printf("%s[1] Server-URL%s\n", bold, reset)
	fmt.Printf("    %sBeispiel: http://192.168.1.100:8765%s\n", dim, reset)
	if cfg.Server != "" {
		fmt.Printf("    Aktuell: %s%s%s\n", cyan, cfg.Server, reset)
		fmt.Printf("    Enter = beibehalten: ")
	} else {
		fmt.Printf("    Server-URL: ")
	}
	input := readLine(r)
	if input != "" {
		cfg.Server = strings.TrimRight(input, "/")
	}
	if cfg.Server == "" {
		fmt.Fprintf(os.Stderr, "%sFehler: Server-URL darf nicht leer sein.%s\n", yellow, reset)
		os.Exit(1)
	}

	// --- Token ---
	fmt.Printf("\n%s[2] Authentifizierungs-Token%s\n", bold, reset)
	fmt.Printf("    %sDer Token wird in der MiniAssistant-Config unter server.token gesetzt.%s\n", dim, reset)
	fmt.Printf("    %sOhne Token: Verbindung läuft offen (nur in vertrauenswürdigen Netzen).%s\n", dim, reset)
	if cfg.Token != "" {
		fmt.Printf("    Aktuell: [gesetzt]\n")
		fmt.Printf("    Enter = beibehalten, '-' = entfernen: ")
	} else {
		fmt.Printf("    Token (oder Enter für kein Token): ")
	}
	input = readLine(r)
	if input == "-" {
		cfg.Token = ""
	} else if input != "" {
		cfg.Token = input
	}

	// --- Modell ---
	fmt.Printf("\n%s[3] Modell%s\n", bold, reset)
	fmt.Printf("    %sDas Modell, das der Server verwenden soll. Leer = Serverstandard.%s\n", dim, reset)
	fmt.Printf("    %sBeispiel: qwen3:8b, deepseek-r1:7b%s\n", dim, reset)
	if cfg.Model != "" {
		fmt.Printf("    Aktuell: %s\n", cfg.Model)
		fmt.Printf("    Enter = beibehalten, '-' = Serverstandard: ")
	} else {
		fmt.Printf("    Modell (oder Enter für Serverstandard): ")
	}
	input = readLine(r)
	if input == "-" {
		cfg.Model = ""
	} else if input != "" {
		cfg.Model = input
	}

	// --- Lokale Tools (pro Tool einzeln) ---
	fmt.Printf("\n%s[4] Tool-Ausführung%s\n", bold, reset)
	fmt.Printf("    %sStandard: serverseitig. Lokal = auf DIESEM Rechner ausführen.%s\n", dim, reset)
	fmt.Printf("    %sAchtung: lokales exec führt Befehle direkt auf diesem System aus.%s\n\n", yellow, reset)

	newLocalTools := []string{}
	for _, t := range availableLocalTools {
		isLocal := false
		for _, lt := range cfg.LocalTools {
			if lt == t.Name {
				isLocal = true
				break
			}
		}
		cur := "Server"
		if isLocal {
			cur = "lokal "
		}
		fmt.Printf("    %s%-10s%s – %s\n", bold, t.Name, reset, t.Description)
		fmt.Printf("    Aktuell: %s  [S=Server / l=Lokal, Enter=beibehalten]: ", cur)
		ans := strings.ToLower(strings.TrimSpace(readLine(r)))
		switch ans {
		case "l", "lokal", "local":
			newLocalTools = append(newLocalTools, t.Name)
		case "s", "server", "serverseitig":
			// server-seitig, nicht hinzufügen
		default: // Enter = beibehalten
			if isLocal {
				newLocalTools = append(newLocalTools, t.Name)
			}
		}
		fmt.Println()
	}
	cfg.LocalTools = newLocalTools

	// --- Proxy (optional) ---
	fmt.Printf("\n%s[5] Proxy für lokale read_url-Aufrufe%s %s(optional)%s\n", bold, reset, dim, reset)
	fmt.Printf("    %sUnterstützt: http://host:port  https://host:port  socks5://host:port%s\n", dim, reset)
	fmt.Printf("    %sSocks5 mit Auth: socks5://user:pass@host:port%s\n", dim, reset)
	if cfg.Proxy != "" {
		fmt.Printf("    Aktuell: %s%s%s\n", cyan, cfg.Proxy, reset)
		fmt.Printf("    Enter = beibehalten, '-' = entfernen: ")
	} else {
		fmt.Printf("    Proxy-URL (oder Enter für keinen): ")
	}
	input = readLine(r)
	if input == "-" {
		cfg.Proxy = ""
	} else if input != "" {
		cfg.Proxy = input
	}

	// --- Speichern ---
	if err := saveConfig(cfg); err != nil {
		fmt.Fprintf(os.Stderr, "%sFehler beim Speichern: %v%s\n", yellow, err, reset)
		os.Exit(1)
	}
	fmt.Printf("\n%sKonfiguration gespeichert:%s %s\n", green, reset, configPath)
	fmt.Printf("  Server:     %s\n", cfg.Server)
	if cfg.Token != "" {
		fmt.Printf("  Token:      [gesetzt]\n")
	} else {
		fmt.Printf("  Token:      %s(kein Token)%s\n", yellow, reset)
	}
	if cfg.Model != "" {
		fmt.Printf("  Modell:     %s\n", cfg.Model)
	}
	if len(cfg.LocalTools) > 0 {
		fmt.Printf("  Lok. Tools: %s\n", strings.Join(cfg.LocalTools, ", "))
	} else {
		fmt.Printf("  Lok. Tools: (alle serverseitig)\n")
	}
	if cfg.Proxy != "" {
		fmt.Printf("  Proxy:      %s\n", cfg.Proxy)
	}
}

// ---------------------------------------------------------------------------
// HTTP-Hilfsfunktionen
// ---------------------------------------------------------------------------

func newRequest(method, url, token string, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequest(method, url, body)
	if err != nil {
		return nil, err
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return req, nil
}

func postJSON(server, token, path string, payload any) (*http.Response, error) {
	data, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	req, err := newRequest("POST", server+path, token, bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	return http.DefaultClient.Do(req)
}

// ---------------------------------------------------------------------------
// HTTP-Client mit optionalem Proxy (http://, https://, socks5://)
// ---------------------------------------------------------------------------

func httpClientWithProxy(proxyURL string, timeout time.Duration) *http.Client {
	if proxyURL == "" {
		return &http.Client{Timeout: timeout}
	}
	u, err := url.Parse(proxyURL)
	if err != nil {
		return &http.Client{Timeout: timeout}
	}
	return &http.Client{
		Transport: &http.Transport{Proxy: http.ProxyURL(u)},
		Timeout:   timeout,
	}
}

// ---------------------------------------------------------------------------
// Lokale Tool-Ausführung
// ---------------------------------------------------------------------------

func execLocalTool(toolName string, args map[string]any, proxy string) string {
	switch toolName {
	case "exec":
		cmd, _ := args["command"].(string)
		if cmd == "" {
			return "Error: no command provided"
		}
		shell := "bash"
		if runtime.GOOS == "windows" {
			shell = "cmd"
		}
		var shellArgs []string
		if runtime.GOOS == "windows" {
			shellArgs = []string{"/C", cmd}
		} else {
			shellArgs = []string{"-c", cmd}
		}
		out, err := exec.Command(shell, shellArgs...).CombinedOutput()
		if err != nil {
			return string(out) + "\nExit: " + err.Error()
		}
		return string(out)

	case "read_url":
		url, _ := args["url"].(string)
		if url == "" {
			return "Error: no url provided"
		}
		client := httpClientWithProxy(proxy, 30*time.Second)
		resp, err := client.Get(url)
		if err != nil {
			return "Error: " + err.Error()
		}
		defer resp.Body.Close()
		body, err := io.ReadAll(io.LimitReader(resp.Body, 512*1024))
		if err != nil {
			return "Error reading body: " + err.Error()
		}
		return string(body)

	default:
		return fmt.Sprintf("Error: tool '%s' not supported locally", toolName)
	}
}

// ---------------------------------------------------------------------------
// Spinner
// ---------------------------------------------------------------------------

func startSpinnerTo(f *os.File) func() {
	fi, err := f.Stat()
	if err != nil || (fi.Mode()&os.ModeCharDevice) == 0 {
		return func() {}
	}
	quit := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		frames := []string{"⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"}
		i := 0
		ticker := time.NewTicker(100 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-quit:
				fmt.Fprintf(f, "\r\033[K") // Zeile löschen
				return
			case <-ticker.C:
				fmt.Fprintf(f, "\r\033[2m%s …\033[0m", frames[i%len(frames)])
				i++
			}
		}
	}()
	stopped := false
	return func() {
		if !stopped {
			stopped = true
			close(quit)
			<-done
		}
	}
}

func startSpinner() func() { return startSpinnerTo(os.Stdout) }

// ---------------------------------------------------------------------------
// Stream-Chat
// ---------------------------------------------------------------------------

type streamEvent struct {
	Type        string           `json:"type"`
	Delta       string           `json:"delta"`
	Message     string           `json:"message"`
	Tools       []string         `json:"tools"`
	SessionID   string           `json:"session_id"`
	Content     string           `json:"content"`
	Thinking    string           `json:"thinking"`
	Error       string           `json:"error"`
	TPS         []any            `json:"tps"`
	NewMessages []map[string]any `json:"new_messages,omitempty"`
	Ctx         []any            `json:"ctx,omitempty"` // [used_tokens, max_tokens]
	// tool_request fields
	ID   string         `json:"id"`
	Tool string         `json:"tool"`
	Args map[string]any `json:"args"`
}

func runChat(cfg Config, preload *Session) {
	fmt.Printf("%sMiniAssistant%s → %s%s%s\n", bold, reset, cyan, cfg.Server, reset)
	if cfg.Token == "" {
		fmt.Printf("%sWarnung: Kein Token — Verbindung läuft ohne Authentifizierung.%s\n", yellow, reset)
	}
	if len(cfg.LocalTools) > 0 {
		fmt.Printf("%sLokale Tools: %s%s\n", dim, strings.Join(cfg.LocalTools, ", "), reset)
	}

	// Verbindung testen
	req, _ := newRequest("GET", cfg.Server+"/v1/models", cfg.Token, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%sWarnung: Server nicht erreichbar — %s%s\n\n", yellow, cfg.Server, reset)
	} else {
		resp.Body.Close()
		if resp.StatusCode == 401 {
			fmt.Fprintf(os.Stderr, "%sFehler: Token ungültig (HTTP 401).%s\n", yellow, reset)
			os.Exit(1)
		}
	}

	// readline für Eingaben (Pfeiltasten, History, Ctrl+A/E/K/W)
	histFile := filepath.Join(sessionsDir, ".history")
	os.MkdirAll(sessionsDir, 0700)
	rl, rlErr := readline.NewEx(&readline.Config{
		Prompt:          bold + green + "Du: " + reset,
		HistoryFile:     histFile,
		HistoryLimit:    500,
		InterruptPrompt: "^C",
		EOFPrompt:       "exit",
	})
	if rlErr == nil {
		defer rl.Close()
	}

	// readPrompt: für einfache Ja/Nein-Fragen (Prompt temporär wechseln)
	readPrompt := func(prompt string) string {
		if rl != nil {
			rl.SetPrompt(prompt)
			line, err := rl.Readline()
			rl.SetPrompt(bold + green + "Du: " + reset)
			if err != nil {
				return ""
			}
			return strings.TrimSpace(line)
		}
		fmt.Print(prompt)
		r := bufio.NewReader(os.Stdin)
		line, _ := r.ReadString('\n')
		return strings.TrimRight(line, "\r\n")
	}

	// --- Session laden ---
	sessionID := ""
	var sessionMessages []map[string]any
	serverHasSession := true // false = erste Runde einer wiederhergestellten Session

	if preload != nil {
		sessionID = preload.ID
		sessionMessages = preload.Messages
		serverHasSession = false
		preview := preload.Preview
		if preview == "" && len(preload.ID) >= 8 {
			preview = preload.ID[:8] + "…"
		}
		fmt.Printf("\n%s✓ Session wird fortgesetzt:%s %s\n", green, reset, preview)
	} else {
		sessions := listSessions()
		if len(sessions) > 0 {
			last := sessions[0]
			updatedAt := ""
			if last.UpdatedAt != "" {
				if t, err := time.Parse(time.RFC3339, last.UpdatedAt); err == nil {
					updatedAt = t.Format("02.01. 15:04")
				}
			}
			preview := last.Preview
			if preview == "" && len(last.ID) >= 8 {
				preview = last.ID[:8] + "…"
			}
			fmt.Printf("\n%s┌─ Vorige Session wiederaufnehmen%s\n", bold, reset)
			fmt.Printf("%s│%s  %s%s%s  –  %s\n", bold, reset, dim, updatedAt, reset, preview)
			fmt.Printf("%s└%s\n", bold, reset)
			ans := readPrompt("Fortsetzen? [j/N]: ")
			ans = strings.ToLower(ans)
			if ans == "j" || ans == "ja" || ans == "y" || ans == "yes" {
				sessionID = last.ID
				sessionMessages = last.Messages
				serverHasSession = false
				fmt.Printf("%s✓ Session wird fortgesetzt.%s\n", green, reset)
			} else {
				fmt.Printf("%s✓ Neue Session gestartet.%s\n", green, reset)
			}
		} else {
			fmt.Printf("%s✓ Neue Session gestartet.%s\n", green, reset)
		}
	}
	fmt.Printf("\n%sBefehle:%s\n", dim, reset)
	fmt.Printf("%s  /model          Aktuelles Modell anzeigen%s\n", dim, reset)
	fmt.Printf("%s  /model NAME     Modell wechseln%s\n", dim, reset)
	fmt.Printf("%s  /new            Neue Session starten%s\n", dim, reset)
	fmt.Printf("%s  exit            Beenden%s\n\n", dim, reset)

	client := &http.Client{Timeout: 0} // kein globaler Timeout — Stream kann lange laufen
	var titleCh chan string            // Titel-Goroutine, nur für neue Sessions
	var generatedTitle string          // einmal gesetzt, immer bevorzugt

	for {
		// Eingabe mit readline (Pfeiltasten, History)
		var input string
		if rl != nil {
			rl.SetPrompt(bold + green + "Du: " + reset)
			line, err := rl.Readline()
			if err == readline.ErrInterrupt {
				break // Ctrl+C → beenden
			}
			if err != nil {
				// EOF (Ctrl+D) oder anderer Fehler → beenden
				break
			}
			input = strings.TrimSpace(line)
		} else {
			// Fallback ohne readline
			fmt.Printf("%s%sDu: %s", bold, green, reset)
			r := bufio.NewReader(os.Stdin)
			line, _ := r.ReadString('\n')
			input = strings.TrimRight(line, "\r\n")
		}

		if input == "" {
			continue
		}
		if input == "exit" || input == "quit" || input == "q" {
			break
		}

		// Titel parallel generieren — nur einmal, für die allererste Nachricht einer neuen Session
		if sessionID == "" && len(sessionMessages) == 0 && titleCh == nil {
			titleCh = make(chan string, 1)
			go func(msg string) {
				if t := fetchTitle(cfg, msg); t != "" {
					titleCh <- t
				}
			}(input)
		}

		// Request aufbauen
		payload := map[string]any{
			"message": input,
		}
		if sessionID != "" {
			payload["session_id"] = sessionID
			if !serverHasSession && len(sessionMessages) > 0 {
				payload["restore_messages"] = sessionMessages
			}
		}
		if cfg.Model != "" {
			payload["model"] = cfg.Model
		}
		if len(cfg.LocalTools) > 0 {
			payload["local_tools"] = cfg.LocalTools
		}

		data, _ := json.Marshal(payload)
		req, err := newRequest("POST", cfg.Server+"/api/chat/stream", cfg.Token, bytes.NewReader(data))
		if err != nil {
			fmt.Fprintf(os.Stderr, "%sFehler: %v%s\n", yellow, err, reset)
			continue
		}

		streamResp, err := client.Do(req)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%sFehler: %v%s\n", yellow, err, reset)
			continue
		}

		// Spinner: kann nach Denkvorgang neu gestartet werden
		var currentStop func() = func() {}
		spinActive := false
		launchSpinner := func() {
			spinActive = true
			currentStop = startSpinner()
		}
		stopSpinOnce := func() {
			if spinActive {
				spinActive = false
				currentStop()
			}
		}
		launchSpinner() // initial starten

		hadThinking := false
		contentStarted := false
		var contentBuf strings.Builder
		atLineStart := true // Cursor am Zeilenanfang?

		ensureNewLine := func() {
			if !atLineStart {
				fmt.Println()
				atLineStart = true
			}
		}

		scanner := bufio.NewScanner(streamResp.Body)
		scanner.Buffer(make([]byte, 256*1024), 256*1024)

		for scanner.Scan() {
			line := scanner.Text()
			if line == "" {
				continue
			}
			var ev streamEvent
			if err := json.Unmarshal([]byte(line), &ev); err != nil {
				continue
			}

			switch ev.Type {
			case "thinking":
				stopSpinOnce() // Spinner stoppen, bevor sichtbare Ausgabe kommt
				if !hadThinking {
					ensureNewLine()
					fmt.Printf("%s▸ Denkvorgang%s\n", dim, reset)
					hadThinking = true
					atLineStart = true
				}
				fmt.Printf("%s%s%s", dim, ev.Delta, reset)
				atLineStart = strings.HasSuffix(ev.Delta, "\n")

			case "content":
				// Inhalt wird erst bei "done" ausgegeben
				// Nach Denkvorgang: Spinner für Content-Phase neu starten
				if hadThinking && !contentStarted {
					contentStarted = true
					ensureNewLine()
					launchSpinner()
				}
				contentBuf.WriteString(ev.Delta)

			case "status":
				stopSpinOnce()
				ensureNewLine()
				fmt.Printf("%s[%s]%s\n", dim, ev.Message, reset)
				atLineStart = true

			case "tool_call":
				stopSpinOnce()
				ensureNewLine()
				fmt.Printf("%s[Tool: %s]%s\n", dim, strings.Join(ev.Tools, ", "), reset)
				atLineStart = true

			case "tool_request":
				stopSpinOnce()
				ensureNewLine()
				fmt.Printf("%s[Lokal: %s]%s\n", dim, ev.Tool, reset)
				atLineStart = true
				result := execLocalTool(ev.Tool, ev.Args, cfg.Proxy)
				toolPayload := map[string]any{"tool_id": ev.ID, "result": result}
				tresp, err := postJSON(cfg.Server, cfg.Token, "/api/chat/tool_result", toolPayload)
				if err != nil {
					fmt.Fprintf(os.Stderr, "%sTool-Result-Fehler: %v%s\n", yellow, err, reset)
				} else {
					tresp.Body.Close()
				}

			case "done":
				stopSpinOnce()
				if ev.SessionID != "" {
					sessionID = ev.SessionID
					serverHasSession = true
				}
				if len(ev.NewMessages) > 0 {
					sessionMessages = ev.NewMessages
				}
				if sessionID != "" {
					// Titel aus Goroutine holen falls fertig
					if titleCh != nil {
						select {
						case t := <-titleCh:
							generatedTitle = t
							titleCh = nil
						default:
						}
					}
					preview := generatedTitle
					if preview == "" {
						preview = sessionPreview(sessionMessages)
					}
					saveSession(Session{
						ID:        sessionID,
						Messages:  sessionMessages,
						UpdatedAt: time.Now().UTC().Format(time.RFC3339),
						Preview:   preview,
					})
				}

				content := contentBuf.String()
				if hadThinking {
					ensureNewLine()
					fmt.Println() // Leerzeile nach Denkvorgang
				}
				if content != "" {
					tpsLabel := ""
					if len(ev.TPS) > 0 {
						tpsVal, _ := ev.TPS[0].(float64)
						tpsExact, _ := ev.TPS[1].(bool)
						if tpsVal > 0 {
							pfx := "~"
							if tpsExact {
								pfx = ""
							}
							tpsLabel = fmt.Sprintf(" %s(%s%d t/s)%s", dim, pfx, int(tpsVal), reset)
						}
					}
					fmt.Printf("\n%s%sAssistant%s%s%s:%s\n", bold, green, reset, tpsLabel, bold+green, reset)
					fmt.Println(content)
					atLineStart = true
				}
				if ev.Error != "" {
					ensureNewLine()
					fmt.Printf("%sFehler: %s%s\n", yellow, ev.Error, reset)
				}
				// Kontext-Auslastung anzeigen (nur bei echten Antworten)
				if content != "" && len(ev.Ctx) >= 2 {
					ctxUsed, _ := ev.Ctx[0].(float64)
					ctxMax, _ := ev.Ctx[1].(float64)
					if ctxMax > 0 {
						ctxPct := int(ctxUsed * 100 / ctxMax)
						if ctxPct > 100 {
							ctxPct = 100
						}
						filled := ctxPct * 10 / 100
						bar := ""
						for i := 0; i < 10; i++ {
							if i < filled {
								bar += "█"
							} else {
								bar += "░"
							}
						}
						ctxColor := dim
						if ctxPct >= 85 {
							ctxColor = "\033[31m" // rot
						} else if ctxPct >= 70 {
							ctxColor = yellow
						}
						ctxLabel := fmt.Sprintf("%s %d%% ctx", bar, ctxPct)
						pad := getTermWidth() - len([]rune(ctxLabel))
						if pad < 0 {
							pad = 0
						}
						fmt.Printf("%s%s%s%s\n", strings.Repeat(" ", pad), ctxColor, ctxLabel, reset)
					}
				}
			}
		}
		stopSpinOnce()
		streamResp.Body.Close()
		fmt.Println()
	}

	// Session speichern falls noch nicht geschehen (z.B. Ctrl+C während Streaming)
	if sessionID != "" && len(sessionMessages) > 0 {
		// Auf Titel warten falls Goroutine noch läuft (max 8s)
		if titleCh != nil {
			select {
			case t := <-titleCh:
				generatedTitle = t
			case <-time.After(8 * time.Second):
			}
		}
		preview := generatedTitle
		if preview == "" {
			preview = sessionPreview(sessionMessages)
		}
		saveSession(Session{
			ID:        sessionID,
			Messages:  sessionMessages,
			UpdatedAt: time.Now().UTC().Format(time.RFC3339),
			Preview:   preview,
		})
	}

	fmt.Printf("%sAuf Wiedersehen.%s\n", dim, reset)
}

// ---------------------------------------------------------------------------
// Hilfsfunktionen
// ---------------------------------------------------------------------------

func readLine(r *bufio.Reader) string {
	line, _ := r.ReadString('\n')
	return strings.TrimRight(line, "\r\n")
}

// ---------------------------------------------------------------------------
// Einzel-Frage (--question / -q): eine Frage stellen, Antwort auf stdout, fertig
// ---------------------------------------------------------------------------

func runQuestion(cfg Config, question string) {
	payload := map[string]any{"message": question}
	if cfg.Model != "" {
		payload["model"] = cfg.Model
	}

	data, _ := json.Marshal(payload)
	req, err := newRequest("POST", cfg.Server+"/api/chat/stream", cfg.Token, bytes.NewReader(data))
	if err != nil {
		fmt.Fprintf(os.Stderr, "Fehler: %v\n", err)
		os.Exit(1)
	}

	client := &http.Client{Timeout: 0}
	resp, err := client.Do(req)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Fehler: %v\n", err)
		os.Exit(1)
	}
	defer resp.Body.Close()

	// Spinner auf stderr; wenn kein Terminal → einfache Textausgabe
	stopSpin := startSpinnerTo(os.Stderr)
	if fi, e := os.Stderr.Stat(); e != nil || (fi.Mode()&os.ModeCharDevice) == 0 {
		fmt.Fprintln(os.Stderr, "Denke...")
	}

	var contentBuf strings.Builder
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 256*1024), 256*1024)

	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}
		var ev streamEvent
		if json.Unmarshal([]byte(line), &ev) != nil {
			continue
		}
		switch ev.Type {
		case "content":
			contentBuf.WriteString(ev.Delta)
		case "done":
			stopSpin()
			content := contentBuf.String()
			fmt.Print(content)
			if !strings.HasSuffix(content, "\n") {
				fmt.Println()
			}
			if ev.Error != "" {
				fmt.Fprintf(os.Stderr, "Fehler: %s\n", ev.Error)
				os.Exit(1)
			}
			return
		case "error":
			stopSpin()
			fmt.Fprintf(os.Stderr, "Fehler: %s\n", ev.Error)
			os.Exit(1)
		}
	}
	stopSpin()
}

func printUsage() {
	fmt.Printf(`%sMiniAssistant Go-Client%s

Nutzung:
  maclient                  Chat starten
  maclient config           Konfiguration erstellen/bearbeiten
  maclient config --show    Aktuelle Config anzeigen
  maclient --sessions       Alle Sessions anzeigen
  maclient --continue       Session auswählen und fortsetzen
  maclient --continue N     Session N fortsetzen (Nummer aus --sessions)
  maclient --continue ID    Session per ID-Prefix fortsetzen
  maclient --question TEXT  Einzel-Frage stellen, Antwort auf stdout, beenden
  maclient -q TEXT          Kurzform von --question
  echo "..." | maclient -q  Frage per Pipe übergeben

Config-Datei: %s

Umgebungsvariablen (überschreiben Config):
  MINIASSISTANT_URL     Server-URL
  MINIASSISTANT_TOKEN   Token
  MINIASSISTANT_MODEL   Modell

`, bold, reset, configPath)
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

func main() {
	initColors()
	migrateOldSession()

	args := os.Args[1:]

	if len(args) > 0 && args[0] == "config" {
		showOnly := len(args) > 1 && args[1] == "--show"
		runConfig(showOnly)
		return
	}

	if len(args) > 0 && (args[0] == "-h" || args[0] == "--help") {
		printUsage()
		return
	}

	if len(args) > 0 && args[0] == "--sessions" {
		sessions := listSessions()
		showSessions(sessions)
		return
	}

	// Config laden (Umgebungsvariablen haben Vorrang)
	cfg, err := loadConfig()
	if err != nil && !os.IsNotExist(err) {
		fmt.Fprintf(os.Stderr, "%sWarnung: Config-Fehler: %v%s\n", yellow, err, reset)
	}

	if v := os.Getenv("MINIASSISTANT_URL"); v != "" {
		cfg.Server = strings.TrimRight(v, "/")
	}
	if v := os.Getenv("MINIASSISTANT_TOKEN"); v != "" {
		cfg.Token = v
	}
	if v := os.Getenv("MINIASSISTANT_MODEL"); v != "" {
		cfg.Model = v
	}

	if cfg.Server == "" {
		fmt.Fprintf(os.Stderr, "%sKeine Server-URL konfiguriert.%s\n\n", yellow, reset)
		fmt.Fprintf(os.Stderr, "Ersteinrichtung: %smaclient config%s\n", bold, reset)
		fmt.Fprintf(os.Stderr, "Oder: %sMINIASSISTANT_URL=http://host:8765 maclient%s\n", dim, reset)
		os.Exit(1)
	}

	// --question / -q: Einzel-Frage, Antwort auf stdout, kein interaktiver Chat
	if len(args) > 0 && (args[0] == "--question" || args[0] == "-q") {
		question := ""
		if len(args) > 1 {
			question = strings.TrimSpace(strings.Join(args[1:], " "))
		} else {
			// Frage von stdin lesen (Pipe-freundlich)
			data, _ := io.ReadAll(os.Stdin)
			question = strings.TrimSpace(string(data))
		}
		if question == "" {
			fmt.Fprintln(os.Stderr, "Keine Frage angegeben.")
			fmt.Fprintln(os.Stderr, "Nutzung: maclient --question \"Frage\"")
			fmt.Fprintln(os.Stderr, "         echo \"Frage\" | maclient -q")
			os.Exit(1)
		}
		runQuestion(cfg, question)
		return
	}

	// --continue: Session vorab auswählen
	var preload *Session
	if len(args) > 0 && args[0] == "--continue" {
		sessions := listSessions()
		if len(sessions) == 0 {
			fmt.Printf("%sKeine gespeicherten Sessions vorhanden.%s\n", yellow, reset)
		} else if len(args) > 1 {
			s := findSession(args[1], sessions)
			if s != nil {
				preload = s
			} else {
				fmt.Fprintf(os.Stderr, "%sSession '%s' nicht gefunden.%s\n", yellow, args[1], reset)
				showSessions(sessions)
				os.Exit(1)
			}
		} else {
			showSessions(sessions)
			// Einfache Eingabe für die Auswahl (readline noch nicht aktiv)
			r := bufio.NewReader(os.Stdin)
			fmt.Printf("\nNummer oder ID eingeben: ")
			query := strings.TrimSpace(readLine(r))
			if query != "" {
				s := findSession(query, sessions)
				if s != nil {
					preload = s
				} else {
					fmt.Fprintf(os.Stderr, "%sSession '%s' nicht gefunden.%s\n", yellow, query, reset)
					os.Exit(1)
				}
			}
		}
	}

	runChat(cfg, preload)
}
