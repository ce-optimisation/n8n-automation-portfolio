# Niche and creator discovery — 16 November 2026

**What this is.** A decision aid for choosing where the clipping engine should focus. It ranks opportunity only:
licensing, permission and creator consent were deliberately **not** scoring criteria (owner instruction). Being
listed here never enables retrieval; the engine's access rules are unchanged.

**How far to trust it.** Scores (0–10 per criterion) are my assessments from the sources cited, observed on
16 Nov 2026. Audience figures come from third-party trackers (TwitchTracker, Streams Charts, StreamMetrix,
Streamer Times, Wikipedia, ranking blogs) that **disagree with each other**; ranges are given and windows stated.
RPM figures are directional only (sources differ 2–10×). Channel URLs marked unverified in the dataset must be
confirmed with **Creators → Check via official API** before use. Confidence is stated per niche (low/medium; none
is high). The machine-readable data is in `research_dataset.json`, which the engine loads into the
**Research** page and the **Results → Niche scorecard**, where every score can be edited.

Scoring model (`clipengine/research.py`): **audience** = demand, short-form potential, material supply, clippable
frequency, competition (inverse), proven short-form channels. **Profit** = advertiser value, creator-service demand,
affiliate/lead-gen, revenue beyond payouts, policy risk (inverse), plus operating cost (editing, automation fit,
compute, oversight) and synergy with the owner's businesses. **Overall = 0.4 × audience + 0.6 × profit**, so
reach alone cannot win.

## A. Niche ranking (22 niches and sub-niches)

| # | Niche | Overall | Audience | Profit | Confidence |
|---|---|---|---|---|---|
| 1 | AI, automation & SaaS business | **79** | 72 | **84** | medium |
| 2 | Business podcasts | **77** | 72 | **80** | medium |
| 3 | Entrepreneurship | 72 | 75 | 70 | medium |
| 4 | Consumer technology | 71 | 69 | 72 | medium |
| 5 | Tech buying advice | 70 | 66 | 73 | low |
| 6 | Personal finance & investing | 70 | 69 | 70 | medium |
| 7 | General podcasts & interviews | 69 | 78 | 63 | medium |
| 8 | Gaming hardware | 69 | 66 | 71 | medium |
| 9 | Luxury watches & collecting | 69 | 60 | **74** | low |
| 10 | Property & estate agencies | 67 | 60 | 71 | low |
| 11 | Education & professional skills | 66 | 67 | 66 | low |
| 12 | Automotive (overlooked) | 66 | 67 | 66 | low |
| 13 | IRL livestream entertainment | 63 | **84** | 49 | medium |
| 14 | Fitness & health (emerging) | 63 | 68 | 59 | low |
| 15 | Gaming livestream highlights | 62 | **79** | 51 | medium |
| 16 | Personal development | 62 | 66 | 60 | low |
| 17 | Travel | 57 | 55 | 58 | low |
| 18 | Football & sports creators | 57 | 70 | 48 | low |
| 19 | Psychology, philosophy, theology | 56 | 62 | 52 | low |
| 20 | Political reporting & analysis | 52 | 73 | 37 | medium |
| 21 | Reaction streams | 51 | 72 | 37 | low |
| 22 | Streaming news / creator drama | 46 | 67 | 32 | low |

## D. Viewership versus profitability

**Highest reach:** IRL streams (84), gaming streams (79), general podcasts (78), entrepreneurship (75), politics (73).
**Highest profit:** AI/business (84), business podcasts (80), watches (74), tech buying advice (73), consumer tech (72).

The big livestream personalities generate the most views, but as an **own-channel** business they monetise poorly:
gaming/entertainment Shorts RPM is consistently estimated at a fraction of finance/tech (sources put finance 5–10×
gaming). Their commercial value is in **Model A** (creator-authorised clipping): the paid clipping-campaign market
is large and active — a survey of 719 live campaigns (17 Oct 2026) found a **median of $1 per 1,000 views**, middle
half $1–2 — paid by the creator or brand, not the platform. That changes the ranking: for livestream creators the
question is "who runs a paid clipping programme or would hire an editor", not "what is the RPM".

## E. Recommended starting portfolio

1. **AI, automation & business podcasts** (profiles `ai_business`, `podcasts`) — Model B own channel *and* Model A
   services. Best transcript fit for the engine, highest profit score, direct synergy with CE Optimisation (each clip
   is also marketing for the automation consultancy). Research candidates: The Diary of a CEO, All-In, Lex Fridman,
   Y Combinator, Alex Hormozi, Liam Ottley, Lenny's Podcast.
2. **Consumer tech & gaming hardware** (`consumer_tech`) — Model B with disclosed affiliate links. Candidates: Linus
   Tech Tips (weekly WAN Show live is long, speech-dense material), MKBHD, Gamers Nexus, Hardware Unboxed,
   JayzTwoCents, Hardware Canucks.
3. **Livestream entertainment as a service** (`irl_streams`, `gaming_streams`) — Model A only: pitch clipping to
   creators who already pay for clips, or join their official campaigns. Highest reach; low own-channel RPM; heavy
   competition; long streams (compute and disk). Candidates by evidence: CaseOh (~59.7K avg, Twitch, Nov 2026),
   TheBurntPeanut (~43K avg, #3 hours watched), StableRonaldo (~37.6K avg), Adin Ross (~35–40K avg, Kick),
   Kai Cenat (~34.8K avg, 28-day), N3on (~27–32K avg, Kick), Jynxzi (~27–28K avg, 11.8M followers), IShowSpeed
   (Streamer of the Year ×3; YouTube; no average found).
4. **Luxury watches** (`watches`) — small test. Lowest clip competition among the profitable niches, affluent audience
   and direct synergy with the owner's watch trading; limited long-form supply. Candidates: Teddy Baldassarre,
   Hodinkee (handles and sizes to be confirmed).

Hold for later: **finance** (high RPM, but every clip is a financial-promotion review item) and **automotive** (an
overlooked enthusiast niche with good advertisers).

## B. Named creator shortlist (research candidates, not sources)

Twenty-seven candidates are loaded into the Research page with their figures, windows, sources and an assessment
score (0–100, an opinion). Highlights, by evidence strength:

| Creator | Primary platform | Evidence (window) | Assessment |
|---|---|---|---|
| The Diary of a CEO | YouTube | 12.1M subscribers (Feb 2026) | 82 |
| All-In Podcast | YouTube | >1M subscribers (2026) | 80 |
| Lex Fridman | YouTube | 3.4–5.1M subscribers (sources disagree) | 76 |
| IShowSpeed | YouTube | Streamer of the Year 2026; marathon peak ~460K | 74 |
| Alex Hormozi | YouTube | 3.2M+ subscribers | 74 |
| Kai Cenat | Twitch | ~34.8K average viewers (28 days) | 72 |
| Linus Tech Tips | YouTube | 16.8–17M subscribers | 72 |
| Y Combinator | YouTube | 2.42M subscribers | 72 |
| Liam Ottley | YouTube | 713–814K subscribers | 70 |
| MKBHD | YouTube | 19–21M subscribers | 70 |
| CaseOh | Twitch | ~59.7K avg viewers, 9.23M followers (Nov 2026) | 66 |
| Gamers Nexus | YouTube | 2.59M subscribers (Feb 2026) | 64 |
| Jynxzi | Twitch | ~27–28K avg, 11.8M followers, 292 h streamed (Nov 2026) | 60 |
| StableRonaldo | Twitch | ~37.6K avg, 5.4M followers (Nov 2026) | 58 |
| Adin Ross | Kick | ~35–40K avg (Sep–Oct 2026) | 55 |
| TheBurntPeanut | Twitch | ~43K avg, #3 hours watched (Nov 2026) | 54 |
| N3on | Kick | ~27–32K avg (Sep 2026; 30 days) | 52 |

Not padded: PlaqueBoyMax and xQc are listed with "data unavailable" because no current figures were found;
westcol (#1 on Kick by 7-day hours watched) is Spanish-language; HasanAbi belongs to the political profile.
Per-creator editorial profiles (content type, limitations such as gambling segments, crowds/bystanders, music
or third-party footage) are stored with each candidate.

## C. Platform breakdown (what the engine can actually do)

| Platform | Discovery (official) | Footage acquisition | Status in this build |
|---|---|---|---|
| **Rumble** | No public API; channel page's embedded grid | yt-dlp, only with recorded written permission | **Verified on the owner's PC** (listing, download, auto pick-up of a new upload) |
| **Twitch** | Helix: users, followers, live status/viewers, VODs, clips with view counts, schedule | **Official "Get Clips Download"** (beta since Sep 2025) needs the *broadcaster's or an editor's* authorisation; no official VOD download — yt-dlp only under recorded permission | Adapter implemented; **mock-tested**; READY FOR OWNER CONNECTION (free Twitch app) |
| **Kick** | Public API v1 (app token): channel, live status, viewers, category | No documented VOD/clip export; any retrieval only under permission and unverified | Adapter implemented; **mock-tested**; READY FOR OWNER CONNECTION |
| **YouTube Live** | Data API v3 (API key): channel, subscribers, uploads, live/upcoming via liveStreamingDetails — 3 quota units per check, never search.list | yt-dlp (existing path) under permission | Adapter implemented; **mock-tested**; READY FOR OWNER CONNECTION |

## F. Editorial profiles

Fourteen editable niche profiles ship with the engine (Niches page), including separate IRL, competitive gaming,
reaction, sports, podcast, political (high-risk, never promotional) and finance (compliance review) profiles.
Creator content types change what a complete clip is: IRL = setup → interaction → payoff; competitive gaming =
clutch/decisive moment understandable to non-players; reaction = source context + reaction + conclusion; podcast =
complete argument with the reply; product = comparison/specification/verdict; education = one verifiable concept.

## G. Integration results

See the engineering handoff (`clipping-engine/HANDOFF.md`, §1C) for the verified/mocked/awaiting table.

## H. Further opportunities

- **Model A first for livestreamers.** The paid-campaign market (median ~$1/1,000 views) monetises the reach that
  own-channel Shorts RPM does not. Clippers report post-view rejections and payout holds of up to 90 days — budget for it.
- **Owner's own expertise as the moat.** AI-automation clips double as lead generation for CE Optimisation; add a
  short original commentary hook to excerpts (better monetisation eligibility than pure reposts).
- **Weekly live tech shows** (e.g. LTT's WAN Show) are long, speech-dense and predictable — a strong engine fit.
- **Overlooked:** automotive and watches have fewer clip accounts and premium advertisers.
- **Spanish-language streaming** (westcol leads Kick's 7-day ranking) is a large market that would need a Spanish
  model and a Spanish-speaking reviewer.
- **Avoid:** streaming-news/drama and reaction re-uploads — high reach, lowest profit, highest rights and policy risk.

## Sources (observed 16 Nov 2026)

- Clipping campaign data 2026 — https://clippingcollective.com/guides/clipping-campaign-data-2026
- TwitchTracker, most watched / viewership Nov 2026 — https://twitchtracker.com/channels/hours-watched , https://twitchtracker.com/channels/viewership
- Streams Charts, 7-day ranking; n3on; Streamer Awards 2026; Minecraft marathon — https://streamscharts.com/channels , https://streamscharts.com/channels/n3on?platform=kick , https://streamscharts.com/news/streamer-awards-2026-results , https://streamscharts.com/news/kai-cenat-ishowspeed-2026-minecraft-marathon-recap
- StreamMetrix n3on — https://streammetrix.com/channels/kick/n3on ; Streamer Times — https://streamertimes.tv/rankings/most-watched
- Adin Ross 2026 — https://realplayerwins.com/adin-ross ; https://streamerstats.com/kick/channels/adinross/streams/132367809
- Shorts RPM (directional) — https://www.usekineo.com/youtube-shorts-rpm-by-niche , https://growcreator.pro/blog/youtube-shorts-rpm-2026
- Diary of a CEO / All-In — https://en.wikipedia.org/wiki/The_Diary_of_a_CEO , https://en.wikipedia.org/wiki/All-In_(podcast)
- AI channels/podcasts — https://usefulai.com/feeds/youtube-channels , https://doomers.ai/guides/best-ai-podcasts , https://www.atakinteractive.com/blog/17-ai-youtubers-were-actually-watching-right-now
- Tech/PC hardware channels — https://sup.co/youtubers/tech , https://awisee.com/blog/pc-hardware-youtube-channels/
- Watch channels — https://theslenderwrist.com/youtube-channels/
- Twitch Get Clips Download (official, beta) — https://discuss.dev.twitch.com/t/get-clips-download-twitch-api-endpoint-now-available-in-open-beta/64334
- X API pricing (pay-per-use, no general free tier) — https://www.postproxy.dev/blog/x-api-pricing-2026/
- Facebook Reels publishing (video_state incl. DRAFT) — https://developers.facebook.com/docs/graph-api/reference/page/video_reels
