# Debate — Strukturierte KI-Debatte

Das `debate`-Tool startet eine mehrrundige, strukturierte Diskussion zwischen zwei KI-Perspektiven zu einem Thema. Beide Seiten werden von Subagent-Modellen argumentiert.

## Ablauf

1. **Hauptagent** ruft `debate(topic, perspective_a, perspective_b, model, ...)` auf
2. Für jede Runde:
   - **Seite A** argumentiert (bekommt Zusammenfassung bisheriger Runden + letztes B-Argument)
   - **Seite B** antwortet (bekommt Zusammenfassung + aktuelles A-Argument)
   - Runde wird **automatisch zusammengefasst** → kompakter Kontext für nächste Runde
3. Nach allen Runden: **neutrales Fazit** wird generiert
4. Vollständiges Transkript → Markdown-Datei im Workspace

## Warum Zusammenfassungen?

Kleine Modelle haben begrenzten Kontext. Statt den gesamten Debattenverlauf mitzuschicken (was nach 2-3 Runden den Kontext sprengt), bekommt jede Seite:
- Eine **kompakte Zusammenfassung** aller bisherigen Runden (~150 Wörter)
- Das **letzte Argument** der Gegenseite (vollständig)

So bleibt der Kontext überschaubar, auch bei 5-10 Runden.

## Parameter

| Parameter | Pflicht | Beschreibung |
|-----------|---------|-------------|
| `topic` | ✅ | Das Debattenthema oder die Fragestellung |
| `perspective_a` | ✅ | Position/Standpunkt von Seite A (z.B. "Pro Kernenergie") |
| `perspective_b` | ✅ | Position/Standpunkt von Seite B (z.B. "Contra Kernenergie") |
| `model` | ✅ | Subagent-Modell für Seite A (und B, wenn `model_b` nicht gesetzt) |
| `model_b` | ❌ | Optionales anderes Modell für Seite B |
| `rounds` | ❌ | Anzahl Hin-und-Her-Runden (1-10, Standard: 3) |
| `language` | ❌ | Antwortsprache (Standard: Deutsch) |

## Beispiele

### Gleiche Modelle, verschiedene Perspektiven
```
debate(
  topic="Sollte Deutschland Atomkraftwerke wieder einschalten?",
  perspective_a="Pro Kernenergie: Klimaschutz, Versorgungssicherheit",
  perspective_b="Contra Kernenergie: Sicherheitsrisiken, Endlagerproblematik",
  model="qwen3",
  rounds=3
)
```

### Verschiedene Modelle
```
debate(
  topic="Ist Open Source besser als proprietäre Software?",
  perspective_a="Open Source: Transparenz, Freiheit, Community",
  perspective_b="Proprietär: Support, Integration, Stabilität",
  model="qwen3",
  model_b="ollama-online/gemma3",
  rounds=5,
  language="Deutsch"
)
```

## Output

- **Markdown-Datei** im Workspace: `debate-{thema}-{timestamp}.md`
  - Header mit Metadaten (Modelle, Perspektiven, Runden)
  - Jede Runde: Argument A + Argument B
  - Fazit am Ende
- **Tool-Rückgabe** an den Hauptagent: Zusammenfassung + Dateipfad

## Schutzmechanismen

- **Max 10 Runden** — Hard-Limit verhindert Endlosschleifen
- **Cancellation** — `/stop` oder `/abort` bricht die Debatte sauber ab (bisheriges Transkript bleibt erhalten)
- **Status-Updates** — Bei Matrix/Discord: Fortschrittsmeldungen zwischen den Runden
- **Fehlertoleranz** — Wenn ein Modell-Call fehlschlägt, wird "(Fehler: ...)" eingetragen statt Abbruch

## Tool-Zugriff

Debattierer haben die **gleichen Tools wie normale Subagents**:
- ✅ `web_search` — für aktuelle Informationen (Wetter, News, Preise, Fakten)
- ✅ `exec` — Shell-Befehle (z.B. Dateien inspizieren bei Code-Debatten)
- ✅ `check_url` — URL-Überprüfung

Das bedeutet: Debatten über **aktuelle Themen** funktionieren — die Debattierer können vor dem Argumentieren eine Web-Suche machen, um ihre Position mit aktuellen Daten zu untermauern.

## Kontext-Management (für kleine Modelle)

Jede Seite bekommt pro Runde:
```
System: Rolle + Position + Regeln (~200 Tokens)
User:   Zusammenfassung bisheriger Runden (~150 Wörter)
        + letztes Gegenargument (vollständig, max ~300 Wörter)
```
Gesamt pro Call: ~400-600 Tokens — passt auch in 2K-4K-Kontextmodelle.

## Logging

Bei aktiviertem `server.log_agent_actions`:
- `DEBATE_START` — Topic, Perspektiven, Modelle, Runden
- `DEBATE_ROUND` — Jedes einzelne Argument (A und B)
- `DEBATE_END` — Abschluss mit Rundenzahl und Dateipfad
