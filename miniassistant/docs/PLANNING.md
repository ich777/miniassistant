# Task Planning

## Wann?

Plan erstellen wenn die Aufgabe **3 oder mehr Aktionen** hat (Änderungen, Installationen, Recherche-Schritte etc.).
Auch wenn der User explizit sagt: "mach einen Plan", "plane das".

**Kein Plan** bei: einfachen Fragen, 1-2 Schritten, kurzer Recherche.

---

## Format

**Ort:** `{workspace}/THEMA-plan.md`
**Dateiname:** Kleinbuchstaben, Bindestriche (z.B. `auth-refactoring-plan.md`)

```markdown
# Plan: [Kurzer Titel]

**Ziel:** [Was soll am Ende erreicht sein?]
**Erstellt:** [Datum]

## Schritte

- [ ] 1. Beschreibung
- [ ] 2. Beschreibung
- [ ] 3. Beschreibung

## Notizen

[Erkenntnisse, Entscheidungen während der Arbeit]
```

**Markierungen:** `- [ ]` offen, `- [x]` erledigt, `- [!] Grund` fehlgeschlagen.

---

## Regeln

1. **Plan aktualisieren** nach jedem erledigten Schritt — nicht erst am Ende
2. **Schritte kurz und konkret** — keine vagen Beschreibungen
3. **Neue Schritte einfügen** wenn nötig — bestehenden Plan erweitern, keine neue Datei
4. **Fehler ehrlich markieren** als `- [!]` mit Grund, korrigierten Schritt einfügen
5. **Weitermachen** solange die nächsten Schritte klar sind — nur stoppen wenn User-Input nötig ist
6. **Fortsetzen:** Wenn der User sagt "mach weiter" oder "schau dir den Plan an": Plan lesen, Status zusammenfassen, nächsten offenen Schritt weiterarbeiten

## Abschluss

1. User kurz informieren (max 5-10 Sätze) was erledigt wurde
2. Zusammenfassung in `{workspace}/THEMA-summary.md` schreiben
3. Plan-Datei behalten als Referenz — nur löschen wenn der User es explizit sagt

---

## Subagents

Wenn Subagents verfügbar sind, können eigenständige Schritte delegiert werden.
**Immer Kontext mitgeben** — der Subagent kennt den Plan nicht. Details: siehe `SUBAGENTS.md`.
