# Task Planning – Anleitung für den Assistenten

Dieses Dokument beschreibt, wie du als Assistent **größere Aufgaben** in einen Plan aufteilst, abarbeitest und mit Subagents zusammenarbeitest. Lies dieses Dokument nur, wenn du einen Plan erstellen oder fortsetzen sollst.

---

## Wann einen Plan erstellen?

Erstelle einen Plan wenn:
- Die Aufgabe **mehr als 3 Schritte** hat
- **Mehrere Dateien/Komponenten** betroffen sind
- Der User explizit sagt: "mach einen Plan", "plane das", "erstell einen Plan"
- Du eine komplexe Aufgabe bekommst die du nicht in einer Antwort lösen kannst

**Keinen Plan** bei:
- Einfachen Fragen ("wie wird das Wetter?")
- Einzelne kleine Änderungen (1-2 Schritte)
- Kurze Recherche-Aufgaben

---

## Plan-Datei erstellen

**Ort:** `{workspace}/THEMA-plan.md` (workspace = dein konfiguriertes Arbeitsverzeichnis)
**Dateiname:** Thema in Kleinbuchstaben, Bindestriche, z.B. `refactoring-auth-plan.md`, `migration-db-plan.md`

### Format

```markdown
# Plan: [Kurzer Titel]

**Ziel:** [Was soll am Ende erreicht sein?]
**Erstellt:** [Datum]

## Schritte

- [ ] Schritt 1: Beschreibung
- [ ] Schritt 2: Beschreibung
  - [ ] Teilschritt 2a
  - [ ] Teilschritt 2b
- [ ] Schritt 3: Beschreibung
- [ ] Schritt 4: Tests / Verifizierung

## Notizen

[Erkenntnisse, Entscheidungen, offene Fragen während der Arbeit]
```

### Regeln

1. **Checkliste** mit `- [ ]` (offen) und `- [x]` (erledigt)
2. **Schritte kurz und konkret** — keine vagen Beschreibungen
3. **Notizen-Abschnitt** für Erkenntnisse und Entscheidungen während der Arbeit
4. **Immer den nächsten Schritt** markieren bevor du ihn abarbeitest
5. **Plan aktualisieren** nach jedem erledigten Schritt (exec: Datei überschreiben)
6. **Plan erweitern:** Wenn du während der Arbeit merkst, dass zusätzliche Schritte nötig sind, füge sie in den Plan ein und notiere im Notizen-Abschnitt warum. Keine neue Plan-Datei – bestehende erweitern.
7. **Fehler eingestehen:** Wenn ein Schritt falsch war oder ein Ansatz nicht funktioniert, markiere ihn als `- [!]`, notiere was schiefging, und füge den korrigierten Schritt ein. Fehler korrigieren ist besser als sie zu ignorieren.

---

## Plan abarbeiten

### Ablauf pro Schritt

1. **Plan lesen:** `exec: cat {workspace}/THEMA-plan.md`
2. **Nächsten offenen Schritt** identifizieren
3. **Schritt ausführen**
4. **Plan aktualisieren:** Schritt als erledigt markieren (`- [x]`), ggf. Notizen ergänzen
5. **Status-Update senden** (wenn Chat-Client aktiv): `status_update(message="Schritt 3/7 erledigt: …")` — kurz, 1-2 Sätze
6. **Weiter** mit dem nächsten Schritt

### Wann stoppen, wann weitermachen?

- **Weitermachen** solange die nächsten Schritte klar sind und kein User-Input nötig ist.
- **Stoppen und fragen** wenn du eine Entscheidung brauchst, die der User treffen muss (z.B. "Soll ich Option A oder B nehmen?"). Nutze `status_update` dafür und warte auf Antwort.
- **Zwischeninfos senden** wenn du wichtige Erkenntnisse hast, die der User sofort sehen sollte (z.B. "Sicherheitslücke in Datei X gefunden").

### Zwischen Sessions

Wenn der User sagt "schau dir den Plan an", "mach weiter mit dem Plan", "Plan XY fortsetzen" oder ähnliches:
1. Plan-Datei lesen (`exec: cat`)
2. Status zusammenfassen: was ist erledigt, was steht als nächstes an
3. Weiterarbeiten ab dem nächsten offenen Schritt

### Plan abschließen

Wenn alle Schritte erledigt sind:
1. Dem User das Ergebnis **kurz zusammenfassen** (max 5-10 Sätze)
2. **Zusammenfassung in Datei schreiben:** `exec: cat > {workspace}/THEMA-summary.md << 'EOF' ... EOF` — enthält: was gemacht wurde, Ergebnisse, ggf. offene Punkte
3. Auf die Ergebnis-Datei(en) im Workspace verweisen
4. **Plan-Datei NICHT löschen** — nur löschen wenn der User es **explizit** sagt (z.B. "lösch den Plan", "Plan aufräumen"). Plan-Dateien dienen als Referenz.

---

## Subagents einbinden

Wenn Subagents verfügbar sind (`invoke_model`), kannst du Teilaufgaben delegieren:

### Wann delegieren?

- Der Schritt ist **eigenständig** (Subagents haben eigene Tools: exec, web_search, check_url)
- Ein **spezialisiertes Modell** wäre besser (z.B. Coding-Modell für Code-Review)
- Du brauchst eine **zweite Meinung** oder Verifizierung
- Die Aufgabe lässt sich in **parallele Batches** aufteilen (z.B. "Analysiere Dateien A-C", dann "Analysiere Dateien D-F")

### Wie delegieren?

Gib dem Subagent **Kontext aus dem Plan** mit:

```
invoke_model(
  model="coder",
  message="Kontext: Wir arbeiten an einem Refactoring (Plan-Schritt 3/7).
  Bisherige Änderungen: [kurze Zusammenfassung].
  Aufgabe: [konkreter Teilauftrag].
  Bitte nur die Lösung, keine Erklärung."
)
```

### Regeln für Subagent-Delegation

- **Immer Kontext mitgeben** — der Subagent kennt den Plan nicht
- **Plan-Datei mitgeben:** Wenn ein Plan existiert, sage dem Subagent **explizit** den Pfad zur Plan-Datei und beauftrage ihn, die Checkboxen zu aktualisieren (`- [x]` wenn erledigt, `- [!]` wenn fehlgeschlagen). Beispiel: *"Arbeite gemäß Plan in {workspace}/THEMA-plan.md. Markiere jeden Schritt als [x] wenn erledigt."*
- **Report ≠ Checkliste:** Wenn du einen Bericht/Report erwartest, sage dem Subagent **explizit**: "Schreibe einen Bericht mit konkreten Ergebnissen und Erkenntnissen — keine TODO-Liste." Berichte enthalten Fakten, Analysen, Empfehlungen — keine `- [ ]` Checkboxen.
- **Ergebnis prüfen** bevor du es übernimmst — ist es ein echter Bericht oder nur eine Checkliste?
- **Im Plan notieren** welcher Schritt vom Subagent erledigt wurde
- **Nicht alles delegieren** — du bist verantwortlich für den Gesamtplan
- **Batch-Delegation:** Bei großen Aufgaben den Subagent in mehreren Batches beauftragen statt alles auf einmal. Ergebnis von Batch 1 prüfen, dann Batch 2 starten. So bleibst du in Kontrolle.
- **Ergebnisse in Datei:** Sage dem Subagent, dass er sein Ergebnis in eine Datei im Workspace schreiben soll (z.B. `{workspace}/report-teil1.md`). So gehen keine Daten verloren und du kannst sie später zusammenführen.
- **Subagent meldet Probleme:** Wenn ein Subagent unter `## Suggested plan changes` Änderungen vorschlägt oder Fehler meldet, prüfe diese und entscheide ob der Plan angepasst wird. Du trägst die Verantwortung – übernimm Vorschläge nur wenn sie sinnvoll sind.

---

## Beispiel: Kompletter Plan-Lebenszyklus

**User sagt:** "Refactore die Auth-Module, wir brauchen JWT statt Session-Cookies"

**1. Plan erstellen:**
```markdown
# Plan: Auth-Refactoring JWT

**Ziel:** Session-basierte Auth durch JWT ersetzen
**Erstellt:** February 16, 2026

## Schritte

- [ ] 1. Aktuelle Auth-Architektur analysieren (Dateien, Abhängigkeiten)
- [ ] 2. JWT-Bibliothek auswählen und installieren
- [ ] 3. Token-Generierung implementieren (login endpoint)
- [ ] 4. Token-Validierung als Middleware
- [ ] 5. Session-Code entfernen
- [ ] 6. Tests anpassen
- [ ] 7. Dokumentation aktualisieren

## Notizen

(wird während der Arbeit ergänzt)
```

**2. Abarbeiten:** Schritt für Schritt, Plan nach jedem Schritt updaten.

**3. Subagent einbinden (optional):**
- Schritt 6: `invoke_model(model="coder", message="Review diese JWT-Middleware auf Sicherheitslücken: [code]")`

**4. Abschluss:** Alle `[x]`, User informieren, Plan-Datei löschen.
