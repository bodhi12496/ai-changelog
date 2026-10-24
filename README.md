# AI Changelog

A free, open-source website that logs what's new in AI and data science, every day:
fresh research papers, new GitHub repositories, trending models, tools and demos,
and news. Each entry has a short description and a link to the original.

It runs entirely on GitHub for free: a scheduled GitHub Action collects new items
every 6 hours and publishes a static site to GitHub Pages. No server, database or
API keys needed.

## What it collects

| Category | Sources |
| --- | --- |
| Papers | arXiv (cs.AI, cs.LG, cs.CL, cs.CV, cs.IR, stat.ML), Hugging Face Daily Papers (marked *trending*, with upvotes and AI summaries) |
| Repositories | GitHub repos created in the last 7 days with topics like `llm`, `machine-learning`, `data-science`, `ai-agents`, `rag`, `mcp`, ranked by stars |
| Models | Trending models on Hugging Face |
| Tools and demos | Trending Hugging Face Spaces, AI-related Show HN launches |
| News and posts | AI stories trending on Hacker News, plus blogs from Hugging Face, Google Research, DeepMind, OpenAI, Microsoft Research, AWS ML, BAIR, MIT Technology Review, KDnuggets, Ahead of AI, Import AI and Simon Willison |

The site keeps a rolling 30-day archive.

## Features

- Entries grouped by day, with a 14-day activity strip you can click to jump to a day
- Filter by category, time range and source; full-text search (press `/`)
- Sort by newest or most popular (stars, upvotes and points are normalised per category)
- **New** markers and an "only what's new since my last visit" filter
- Save entries to read later (stored in your browser; they stay even after they leave the feed)
- Shareable filtered views via the URL, light and dark themes, works on phones
- RSS feed at `data/feed.xml` so anyone can subscribe

## Put it online (about 5 minutes)

1. **Create a repository.** On GitHub, create a new **public** repository, for example `ai-changelog`.
2. **Upload the files.** Push this folder to the `main` branch:
   ```bash
   cd ai-changelog
   git init -b main
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/<your-username>/ai-changelog.git
   git push -u origin main
   ```
   (Or use **Add file → Upload files** on GitHub and drag the folder contents in, including the hidden `.github` folder.)
3. **Turn on Pages.** Repository **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. **Allow the workflow to save data.** **Settings → Actions → General → Workflow permissions → Read and write permissions → Save**.
5. **Run it once.** **Actions → Update feed → Run workflow**. After 2 to 4 minutes your site is live at
   `https://<your-username>.github.io/ai-changelog/`.

From then on it refreshes itself every 6 hours.

> GitHub pauses scheduled workflows in repositories with no activity for 60 days. The
> workflow commits fresh data on every run, which normally counts as activity; if it
> ever pauses, press **Enable workflow** in the Actions tab.

### Custom domain (optional)

Add your domain in **Settings → Pages → Custom domain** and point a `CNAME` DNS
record at `<your-username>.github.io`.

### Other free hosts

The site is plain static files in `site/`, so it also works on Netlify, Cloudflare Pages
or Vercel. Point the host at the repo with **no build command** and **`site`** as the
output directory. Keep the GitHub Action running so the data keeps updating; the
host redeploys on each data commit.

## AI summaries (free, no signup)

Repos, tools and news items get a one-sentence summary written by an AI model.
For repos it reads the README, and for Hacker News links it reads the linked page,
so the summary says what the thing actually is. Summaries are marked
**AI summary** on the site; hover the label to see the original description.

**It works out of the box.** The default provider is
[GitHub Models](https://github.com/marketplace/models), which is free for personal
accounts and open-source projects. Inside GitHub Actions it uses the workflow's
built-in token (the workflow already grants `models: read`), so there is no key to
create.

Budget: each run sends at most 10 requests of 10 items each, spaced 5 seconds
apart, which stays well inside GitHub Models' free daily limit for small models.
Each item is summarised once and the summary is kept, so later runs only pay for
new items. If the limit is reached mid-run, the remaining items wait for the next
run.

**Optional backups.** If GitHub Models isn't available to you (for example, an
organisation that has disabled it), add either of these free keys as a repository
secret under **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Where to get it |
| --- | --- |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey); free tier, no credit card |
| `GROQ_API_KEY` | [GroqCloud console](https://console.groq.com/keys); free tier |

Providers are tried in order (GitHub Models, then Gemini, then Groq), and within
each provider the models listed in `sources.json` are tried until one works. Free
model names change over time; if every model fails, the **AI summaries** line in
**Source health** says so, and you can update the `models` list. To switch
summaries off, set `"enabled": false` under `ai_summaries` in `sources.json`.

Note that free tiers may use what you send to improve the provider's products.
That's fine here, since everything sent is already public.

## Customise

Everything about *what* gets collected lives in [`collector/sources.json`](collector/sources.json):

- add or remove RSS feeds (any blog with an RSS or Atom feed works)
- change arXiv categories, GitHub topics, star thresholds, or how many days to keep

Change how often it runs by editing the `cron` line in
[`.github/workflows/update.yml`](.github/workflows/update.yml).

If a source goes down or changes its format, the run still succeeds; the failure
shows under **Source health** in the site's sidebar.

## Run locally

```bash
python collector/collect.py        # Python 3.9+, no packages needed
python -m http.server -d site 8000 # then open http://localhost:8000
```

Set `GITHUB_TOKEN` to a personal access token to avoid GitHub's lower anonymous rate limit.

## Project layout

```
collector/collect.py     fetches, cleans, dedupes and merges items (stdlib only)
collector/summarize.py   one-line AI summaries (any OpenAI-compatible API)
collector/sources.json   which sources to collect from, and summary settings
site/index.html          the website (HTML, CSS, vanilla JS; no build step)
site/data/               generated: items.json, status.json, feed.xml
.github/workflows/       scheduled collect + deploy to GitHub Pages
```

## Contributing

Pull requests are welcome, especially new sources. To add one, write a `fetch_*`
function in `collect.py` that returns items built with `item(...)`, and register it in
`main()`. Keep it standard-library only.

## License

MIT. Entry titles, descriptions and links belong to their original sources; this
project only links to them.
