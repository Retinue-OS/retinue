---
name: messaging-contact-lookup
description: >
  Use this skill whenever the user wants to send a message via WhatsApp, Signal, or Telegram,
  or whenever a contact needs to be found in any of these apps.
  ALWAYS use this skill before sending any message — even if the contact name seems clear.
  This skill tells Claude to search recent conversations first (never the contacts directory),
  and to account for speech recognition errors in the contact's name (phonetic variants, typos,
  mishearings). Trigger on any request like "send a WhatsApp to X", "schreib X auf Signal",
  "Nachricht an X senden", or any message-sending intent involving a person's name.
---

# Messaging Contact Lookup

## Grundprinzip

**Immer zuerst die letzten Konversationen durchsuchen** — niemals direkt im Kontaktverzeichnis suchen. Das Kontaktverzeichnis (`search_contacts`) ist fehleranfällig und findet oft nichts. Die letzten Chats sind zuverlässiger und spiegeln die tatsächlich genutzten Kontakte wider.

---

## Schritt-für-Schritt-Vorgehen

### 1. Spracherkennungsfehler antizipieren

Bevor die Suche startet, überlege: Der Name könnte durch Spracherkennung entstellt sein.
Erstelle mental eine Liste von **Klankvarianten** des genannten Namens:

- Ähnlich klingende Buchstaben: *Vlachopoulou → Flachopoulou, Wlachopoulos, Vlachopulu*
- Verschluckte Silben: *Muller → Müller, Mueller, Muhler*
- Deutsche Aussprache griechischer/fremdsprachiger Namen berücksichtigen
- Häufige STT-Fehler: s/z, sch/sh, -os/-us/-as, -nis/-niss

### 2. Letzte Chats abrufen

**WhatsApp:**
```
whatsapp:list_chats(limit=30, sort_by="last_active")
```

**Signal:**
```
signal:get_recent_chats(limit=30)
```

**Telegram:**
```
telegram:getRecentChats() oder äquivalent
```

### 3. Fuzzy-Matching im Ergebnis

Durchsuche die zurückgegebenen Chats nach:
- Exakter Name
- Alle Klankvarianten (Schritt 1)
- Teilstring-Matches (Vorname, Nachname einzeln)
- Firmennamen als Hinweis (z.B. "Musterpflege Spitex" → Spitex-Mitarbeiterin)

**Beispiel:** User sagt "Maria Vlachopoulou" → suche auch nach *Flachopoulou, Wlachopoulos, Vlacho, Musterpflege*

### 4. Bei mehreren Treffern

Zeige dem User die Kandidaten mit letzter Nachricht als Kontext:
> "Ich habe zwei mögliche Kontakte gefunden:
> 1. **Maria Vlachopoulou** (Musterpflege Spitex) – letzte Nachricht: gestern 21:14
> 2. **M. Flachopoulou** – letzte Nachricht: vor 3 Wochen
> Welche Person meinst du?"

### 5. Nachricht senden

Sobald der JID / die Empfänger-ID gefunden ist, Nachricht senden mit dem entsprechenden Tool:
- `whatsapp:send_message(recipient=JID, message=...)`
- `signal:send_message(recipient=..., message=...)`
- Telegram-Äquivalent

---

## Reihenfolge der Tools (Priorität)

| Priorität | Tool | Wann |
|---|---|---|
| 1 | `list_chats` / `get_recent_chats` | Immer zuerst |
| 2 | Fuzzy-Match im Resultat | Wenn kein exakter Treffer |
| 3 | `search_contacts` | Nur als letzter Ausweg |
| 4 | User fragen | Wenn wirklich nichts gefunden |

---

## Wichtige Regeln

- ❌ **Niemals** direkt `search_contacts` aufrufen, ohne zuerst `list_chats` versucht zu haben
- ✅ Immer mit mindestens `limit=20`, besser `limit=30` suchen
- ✅ Letzten Nachrichtentext als Kontext nutzen (Firma, Inhalt) um richtigen Kontakt zu identifizieren
- ✅ Bei unklaren Namen immer Klankvarianten mitdenken
- ✅ Gilt für **WhatsApp, Signal und Telegram** gleichermassen