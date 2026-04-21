# simon-rss

Tägliche Zusammenfassung von [Simon Willisons Blog](https://simonwillison.net/) als RSS-Feed.

Läuft täglich um 06:00 UTC via GitHub Actions, fasst gestrige Posts mit claude-haiku zusammen (nach Thema gruppiert, auf Deutsch), und committed das Ergebnis als `feed.xml` für GitHub Pages.

## Setup

### 1. Repo forken und GitHub Pages aktivieren

- Fork dieses Repo
- Settings → Pages → Source: "Deploy from a branch" → Branch: `main`, Folder: `/ (root)`
- Feed-URL wird: `https://DEIN-USERNAME.github.io/simon-rss/feed.xml`

### 2. Anthropic API Key hinterlegen

- Settings → Secrets and variables → Actions → New repository secret
- Name: `ANTHROPIC_API_KEY`
- Value: dein Anthropic API Key (erhältlich auf [console.anthropic.com](https://console.anthropic.com))

### 3. Ersten Lauf anstoßen

- Actions → "Daily Simon Digest" → "Run workflow"
- Nach ~60 Sekunden ist `feed.xml` im Repo und der Feed ist abonnierbar

### 4. RSS-Reader konfigurieren

Feed-URL in deinen RSS-Reader eintragen:

```
https://tim-hilde.github.io/simon-rss/feed.xml
```

## Kosten

- GitHub Actions: kostenlos (Free tier: 2000 min/month, dieser Job braucht ~1 min/Tag)
- Anthropic claude-haiku: \~\$0.001/Tag (\~\$0.35/Jahr)

## Lokaler Test

```bash
uv sync
ANTHROPIC_API_KEY=sk-ant-... uv run python summarize.py
```

## Wie es funktioniert

1. Fetch `simonwillison.net/atom/everything/`
2. Filtert auf gestrige Posts (UTC)
3. Lädt jeden Artikel-URL und extrahiert den Volltext
4. Sendet alles an `claude-haiku-4-5` mit der Bitte um eine thematisch gruppierte Zusammenfassung auf Deutsch
5. Schreibt einen neuen RSS-`<item>` in `feed.xml` (max. 30 Einträge = ~1 Monat)
6. Committed und pusht `feed.xml`
