"""Niche and creator editorial profiles: how the shared selection engine judges a transcript.

Three layers combine into one `EditorialPolicy` for each recording, without duplicating the engine:

  NicheProfile    (versioned, editable)  audience, what a complete clip is, durations, hook style,
                                         terms to favour/avoid, automatic rejection rules, claims checks,
                                         caption and post-writing preferences, platform notes
  CreatorProfile  (per creator)          personality, recurring topics, vocabulary (also used as Whisper
                                         hints), speech patterns, known editorial limitations
  EditorialPolicy (combined)             what pipeline.py and segment.py actually use

Nothing here retrieves footage, and nothing here grants permission to use a source: the source
registry and `ingest.classify_url` stay the only gate for retrieval.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields

RISK_LEVELS = ("standard", "elevated", "high")
GROUPS = ("commercial", "audience", "specialist")
REJECT_RULES = {
    "generic_motivation": "Generic motivational talk with no specific, checkable takeaway",
    "no_specifics": "No concrete detail (figures, names, steps, specifications) for an educational/commercial niche",
    "no_payoff": "A reaction or event without its setup and payoff",
    "outrage_bait": "Dominated by insults or inflammatory language rather than substance",
    "too_dependent": "Cannot be understood without material outside the clip",
}
FLAG_RULES = {
    "financial_promotion": "Possible financial promotion: needs compliance review before any use",
    "unverifiable_claim": "Earnings, health or product claim that needs checking",
    "political_context": "Political content: needs contextual review (speaker, date, what was replied)",
    "spoiler_risk": "Possible game spoiler (story, ending, mission or leaked content): check it is not spoiling a new release",
    "sensitive_scene": "Possible sensitive scene (graphic violence, sexual content or self-harm): review before any use",
}
SPOILER = re.compile(r"\b(spoilers?|spoil(?:s|ed|ing)?|the ending|final boss|plot twist|twist ending|story mode|main story|"
                     r"story mission|campaign ending|leaked|leak|dies at the end|post[- ]credits?)\b", re.I)
SENSITIVE_SCENE = re.compile(r"\b(gore|gory|decapitat\w*|beheading|tortur\w*|suicide|self[- ]harm|rape|sexual assault|"
                             r"nude|nudity|graphic (?:violence|scene)|massacre)\b", re.I)


@dataclass
class NicheProfile:
    key: str
    name: str
    group: str = "commercial"
    risk: str = "standard"
    version: int = 1
    active: bool = False                # selectable for creators; never activates any source by itself
    audience: str = ""
    purpose: str = ""
    subjects: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)          # favoured vocabulary (also Whisper hints)
    avoid_terms: list[str] = field(default_factory=list)    # lowers a candidate's score
    complete_clip: str = ""             # what counts as a complete clip in this niche
    hook: str = ""                      # preferred opening
    min_s: float = 15.0
    max_s: float = 150.0
    clips_per_recording: int = 6
    selection_profile: str = "v4"       # v4 (stable default) or v5 (ending-aware); see segment.PROFILES
    reply_required: bool = False        # interviews/debates: keep the reply or qualification
    pacing: str = ""
    context_requirements: str = ""
    reject_rules: list[str] = field(default_factory=list)   # keys of REJECT_RULES
    flag_rules: list[str] = field(default_factory=list)     # keys of FLAG_RULES (review, not rejection)
    specificity_weight: float = 0.0     # reward concrete detail (numbers, names, steps)
    reaction_weight: float = 0.0        # reward a clear setup → reaction/payoff (gaming, IRL)
    min_quality: int = 35               # candidates below this quality score are not rendered
    caption_scale: float = 1.0          # relative to the owner's Anton 73 px reference
    post_style: str = ""
    hashtags: list[str] = field(default_factory=list)
    claims_checks: list[str] = field(default_factory=list)
    platform_notes: dict = field(default_factory=dict)
    compliance_review: bool = False     # outputs must be reviewed for compliance before any publication

    @classmethod
    def from_dict(cls, d: dict) -> "NicheProfile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CreatorProfile:
    personality: str = ""
    recurring_topics: list[str] = field(default_factory=list)
    vocabulary: list[str] = field(default_factory=list)      # names, slang, product names: Whisper hints
    speech_patterns: str = ""
    limitations: str = ""               # known editorial limitations / sensitivities
    content_type: str = ""              # irl | competitive_gaming | reaction | podcast | product | education | ...
    min_s: float | None = None
    max_s: float | None = None
    clips_per_recording: int | None = None
    extra_reject_rules: list[str] = field(default_factory=list)
    avoid_terms: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "CreatorProfile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


# Creator content types change what a good clip is, even inside one niche (the addendum's point:
# gaming streamers are not one editorial category).
CONTENT_TYPES = {
    "irl": dict(reaction_weight=0.10, rules=["no_payoff"],
                note="Natural comedic setups, unexpected interactions, reactions and complete little stories."),
    "competitive_gaming": dict(reaction_weight=0.08, rules=["no_payoff"],
                               note="Clutch plays and decisive outcomes with the reaction, understandable to "
                                    "non-players."),
    "reaction": dict(reaction_weight=0.06, rules=["no_payoff", "too_dependent"],
                     note="Keep what is being reacted to, the reaction and its conclusion."),
    "podcast": dict(reply=True, rules=["too_dependent"],
                    note="Complete arguments, explanations and exchanges with defensible takeaways."),
    "product": dict(specificity=0.10, rules=["no_specifics"],
                    note="Clear comparisons, demonstrations, specifications and buying advice."),
    "education": dict(specificity=0.10, rules=["no_specifics", "too_dependent"],
                      note="One complete, verifiable concept per clip."),
}


def _p(**kw) -> NicheProfile:
    return NicheProfile(**kw)


PRESETS: dict[str, NicheProfile] = {p.key: p for p in [
    # ---- priority commercial niches --------------------------------------------------------
    _p(key="ai_business", name="AI, automation, SaaS & business education", group="commercial",
       audience="Founders, operators, marketers and professionals adopting AI and automation",
       purpose="Practical, specific takeaways that viewers can apply; credible expert explanation",
       subjects=["AI tools", "automation", "SaaS", "workflows", "go-to-market", "pricing", "productivity"],
       terms=["AI", "automation", "agent", "workflow", "SaaS", "revenue", "pricing", "customers", "model",
              "prompt", "API", "integration", "n8n", "Zapier", "ChatGPT", "Claude", "LLM", "startup"],
       avoid_terms=["hustle", "grindset"],
       complete_clip="States a problem, gives a concrete method or example, and lands the takeaway.",
       hook="A specific claim, number or counter-intuitive result in the first sentence.",
       min_s=25, max_s=90, selection_profile="v6", pacing="Tight; one idea per clip",
       context_requirements="Name the tool/company when the speaker refers to 'it'.",
       reject_rules=["generic_motivation", "no_specifics", "too_dependent"],
       flag_rules=["unverifiable_claim"], specificity_weight=0.10, min_quality=40,
       post_style="Lead with the takeaway; plain, credible tone; no hype words.",
       hashtags=["#AI", "#automation", "#SaaS", "#business"],
       claims_checks=["revenue or growth figures", "tool capabilities", "pricing"],
       platform_notes={"youtube_shorts": "Searchable titles naming the tool", "x": "Quote the key line"},
       active=True),
    _p(key="consumer_tech", name="Consumer technology, PC & gaming hardware", group="commercial",
       audience="Buyers researching phones, PCs, GPUs, peripherals and consoles",
       purpose="Help a buyer decide: comparisons, specifications, real-world results, value",
       subjects=["GPUs", "CPUs", "PC builds", "phones", "laptops", "monitors", "peripherals", "consoles"],
       terms=["GPU", "CPU", "RTX", "Radeon", "Ryzen", "Intel", "fps", "benchmark", "price", "performance",
              "thermals", "VRAM", "monitor", "refresh rate", "battery", "camera", "specs", "upgrade"],
       complete_clip="Names the products, gives the comparison or measurement, and the verdict.",
       hook="The verdict or the surprising number first.", min_s=20, max_s=75, selection_profile="v6",
       reject_rules=["no_specifics", "too_dependent"], flag_rules=["unverifiable_claim"],
       specificity_weight=0.12, min_quality=40,
       post_style="Product names and the verdict in the title; note the test conditions.",
       hashtags=["#tech", "#pcgaming", "#gpu", "#techtok"],
       claims_checks=["benchmark figures", "prices (date them)", "availability"],
       platform_notes={"youtube_shorts": "Affiliate links belong in the description, disclosed"},
       active=True),
    _p(key="watches", name="Luxury watches, collecting & watch-market education", group="commercial",
       audience="Collectors, enthusiasts and buyers of luxury and independent watches",
       purpose="Expert knowledge: references, market movements, authenticity, history, buying advice",
       subjects=["Rolex", "Patek Philippe", "Audemars Piguet", "Omega", "independents", "grey market", "auctions"],
       terms=["Rolex", "Patek", "Audemars", "Omega", "Submariner", "Daytona", "Nautilus", "Royal Oak",
              "reference", "movement", "calibre", "dial", "bezel", "retail", "grey market", "auction", "box and papers"],
       complete_clip="Identifies the watch/reference and delivers the insight or market point in full.",
       hook="The watch and the claim (value, rarity, mistake to avoid).", min_s=20, max_s=90,
       selection_profile="v6", reject_rules=["no_specifics", "too_dependent"],
       flag_rules=["unverifiable_claim", "financial_promotion"], specificity_weight=0.10, min_quality=40,
       post_style="Reference numbers and prices dated; no 'investment' promises.",
       hashtags=["#watches", "#rolex", "#watchcollector"],
       claims_checks=["prices and market values (date them)", "authenticity statements"], active=False),
    _p(key="education", name="General education & professional skills", group="commercial",
       audience="Learners and professionals", purpose="Teach one complete, verifiable concept per clip",
       terms=["how", "why", "step", "example", "means", "because"],
       complete_clip="One concept explained end to end with an example.", hook="The question the clip answers.",
       min_s=25, max_s=120, selection_profile="v6", reject_rules=["no_specifics", "too_dependent"],
       flag_rules=["unverifiable_claim"], specificity_weight=0.08, hashtags=["#learn", "#education"]),
    _p(key="property", name="Property, estate agencies & entrepreneurship", group="commercial",
       audience="Buyers, landlords, agents and small-business owners",
       purpose="Concrete property and business know-how; local market facts",
       terms=["property", "mortgage", "rent", "yield", "landlord", "estate agent", "valuation", "stamp duty",
              "deposit", "tenant", "planning", "business", "margin", "cash flow"],
       complete_clip="Gives the rule, figure or method and how it applies.", min_s=20, max_s=90,
       selection_profile="v6", reject_rules=["generic_motivation", "no_specifics"],
       flag_rules=["financial_promotion", "unverifiable_claim"], specificity_weight=0.10,
       compliance_review=True, hashtags=["#property", "#realestate", "#business"],
       claims_checks=["yields and returns", "tax statements (jurisdiction, date)"]),
    # ---- audience-oriented niches --------------------------------------------------------
    _p(key="gaming_streams", name="Gaming livestream highlights", group="audience",
       audience="Gaming and streamer fans", purpose="Complete gameplay moments with the streamer's reaction",
       terms=["clutch", "win", "lost", "let's go", "no way", "bro", "chat", "boss", "ranked", "kill"],
       complete_clip="Setup → decisive event → reaction; understandable without knowing the game.",
       hook="Start just before the event; never on the payoff alone.", min_s=12, max_s=60,
       selection_profile="v6", reject_rules=["no_payoff"], flag_rules=["spoiler_risk", "sensitive_scene"],
       reaction_weight=0.08, min_quality=30, pacing="Fast", post_style="Describe the moment, not clickbait; credit the streamer.",
       hashtags=["#gaming", "#twitch", "#streamer"], active=False),
    _p(key="irl_streams", name="IRL & personality livestream entertainment", group="audience",
       audience="Fans of IRL, challenge and personality streams",
       purpose="Complete comedic or surprising interactions and short stories",
       terms=["chat", "bro", "yo", "no way", "oh my god", "let's go", "crazy", "bruh"],
       avoid_terms=[], complete_clip="A setup, the unexpected interaction or reaction, and its end.",
       hook="The situation in one line; the payoff inside the clip.", min_s=12, max_s=75,
       selection_profile="v6", reject_rules=["no_payoff"], flag_rules=[], reaction_weight=0.10,
       min_quality=30, post_style="Neutral description; never mock bystanders; blur concerns noted for review.",
       hashtags=["#irl", "#streamer", "#kick", "#twitch"],
       claims_checks=["identifiable members of the public (privacy)"], active=False),
    _p(key="reaction_streams", name="Reaction & commentary streams", group="audience",
       audience="Viewers of reaction/commentary streamers",
       purpose="The reaction with enough of the original material to be understood",
       complete_clip="What is being reacted to, the reaction, and its conclusion.", min_s=20, max_s=90,
       selection_profile="v6", reject_rules=["no_payoff", "too_dependent"], reaction_weight=0.06,
       claims_checks=["third-party footage inside the stream (rights)"], hashtags=["#reaction"]),
    _p(key="sports_creators", name="Football & sports creator entertainment", group="audience",
       audience="Football and sports fans following creators",
       purpose="Banter, challenges, predictions and reactions around sport",
       terms=["goal", "penalty", "match", "Premier League", "Champions League", "transfer", "manager"],
       complete_clip="The moment and the reaction or the full prediction/argument.", min_s=15, max_s=75,
       selection_profile="v6", reject_rules=["no_payoff"], reaction_weight=0.06,
       claims_checks=["broadcast match footage inside the stream (rights)"], hashtags=["#football"]),
    _p(key="podcasts", name="General podcasts, interviews & debates", group="audience",
       audience="Podcast and interview audiences", purpose="Complete arguments and insightful exchanges",
       complete_clip="The question, the full answer and any essential reply or qualification.",
       hook="The most interesting claim or question.", min_s=30, max_s=120, selection_profile="v6",
       reply_required=True, reject_rules=["too_dependent"], flag_rules=["unverifiable_claim"],
       specificity_weight=0.04, hashtags=["#podcast"]),
    _p(key="psych_phil_theo", name="Psychology, philosophy & theology", group="audience",
       audience="Thoughtful general audiences", purpose="Clear, faithful explanations of ideas",
       complete_clip="An idea stated, reasoned and concluded in the speaker's own words.", min_s=30, max_s=120,
       selection_profile="v6", reply_required=True, reject_rules=["too_dependent", "generic_motivation"],
       flag_rules=["unverifiable_claim"], hashtags=["#philosophy", "#psychology"]),
    _p(key="travel", name="Travel & destinations", group="audience",
       audience="Travellers and armchair travellers", purpose="Specific places, costs, tips and stories",
       terms=["flight", "hotel", "visa", "price", "city", "beach", "food", "budget"],
       complete_clip="A place-specific tip or story with its point.", min_s=15, max_s=75,
       selection_profile="v6", reject_rules=["no_specifics"], specificity_weight=0.06, hashtags=["#travel"]),
    # ---- higher-risk / specialist niches -------------------------------------------------
    _p(key="politics", name="Political reporting & documentary analysis", group="specialist", risk="high",
       audience="Viewers following political reporting and analysis",
       purpose="Factual, contextualised excerpts for reporting and critical analysis — never promotion",
       complete_clip="The full statement with the context needed to understand it fairly, plus any reply.",
       hook="Who said what, when — plainly.", min_s=20, max_s=150, selection_profile="v6",
       reply_required=True, reject_rules=["outrage_bait", "too_dependent"], flag_rules=["political_context"],
       min_quality=35, post_style="Neutral attribution; date and context; never inflame; no endorsement.",
       claims_checks=["quotations verbatim", "date and setting", "factual claims", "safety flags"],
       compliance_review=True,
       platform_notes={"all": "Strict human review; extremist advocacy is flagged, never promoted"},
       active=True),
    _p(key="finance", name="Personal finance & investing", group="specialist", risk="high",
       audience="People learning about money and investing",
       purpose="Education, not advice: explanations with risks stated",
       terms=["interest", "inflation", "ISA", "pension", "index fund", "stocks", "bonds", "risk", "tax"],
       complete_clip="The concept with its risks/caveats, not just the upside.", min_s=30, max_s=120,
       selection_profile="v6", reply_required=True, reject_rules=["generic_motivation", "no_specifics"],
       flag_rules=["financial_promotion", "unverifiable_claim"], specificity_weight=0.08,
       compliance_review=True, post_style="Educational framing; 'not financial advice'; no return promises.",
       claims_checks=["returns", "guarantees", "regulated-activity language (financial promotion rules)"]),
]}


def niche_reply(content_type: str) -> bool:
    return bool(CONTENT_TYPES.get(content_type, {}).get("reply"))


# ---- quality assessment -------------------------------------------------------------------

MOTIVATIONAL = re.compile(r"\b(believe in yourself|never give up|grind|hustle|mindset|you got this|success is|"
                          r"work hard|dream big|level up your life|no excuses|stay focused|be consistent)\b", re.I)
SPECIFIC = re.compile(r"\d|\b(per ?cent|percent|million|billion|thousand|step|first|second|third|because|"
                      r"for example|compared|versus|vs\.?|cheaper|faster|slower)\b", re.I)
REACTION = re.compile(r"\b(oh my god|omg|no way|let'?s go+|what the|bro+|yo+|holy|wow|ha(ha)+|lol|lmao|"
                      r"insane|crazy|dude|bruh|wait wait|are you serious)\b|!", re.I)
FIN_PROMO = re.compile(r"\b(guaranteed (returns?|profit|income)|can'?t lose|10x|100x|get rich|passive income|"
                       r"financial freedom|risk[- ]free|double your money|buy (it|this) now|not financial advice)\b", re.I)
CLAIM = re.compile(r"\b(i made|we made|made \$?\d|earn(ed|s)? \$?\d|\$\d[\d,]*(k| per| a)|cures?|lose \d+ ?(lbs|pounds|kg)|"
                   r"clinically|proven to|scientifically proven|guarantee[sd]?)\b", re.I)
INFLAMMATORY = re.compile(r"\b(idiots?|morons?|scum|traitors?|vermin|disgusting|destroy(ed)? them|"
                          r"owned|wrecked|humiliat\w*|evil)\b", re.I)
DEPENDENT_START = re.compile(r"^(and|but|so|because|which|that|then|also|this|it|he|she|they)\b", re.I)


@dataclass
class EditorialPolicy:
    niche: NicheProfile
    creator: CreatorProfile = field(default_factory=CreatorProfile)
    creator_name: str = ""

    @property
    def content_type(self) -> str:
        return self.creator.content_type

    @property
    def min_s(self) -> float:
        return float(self.creator.min_s or self.niche.min_s)

    @property
    def max_s(self) -> float:
        return float(self.creator.max_s or self.niche.max_s)

    @property
    def clips_per_recording(self) -> int:
        return int(self.creator.clips_per_recording or self.niche.clips_per_recording)

    @property
    def reply_required(self) -> bool:
        return self.niche.reply_required or niche_reply(self.content_type)

    @property
    def reject_rules(self) -> list[str]:
        ct = CONTENT_TYPES.get(self.content_type, {}).get("rules", [])
        return sorted(set(self.niche.reject_rules) | set(ct) | set(self.creator.extra_reject_rules))

    @property
    def specificity_weight(self) -> float:
        return max(self.niche.specificity_weight, CONTENT_TYPES.get(self.content_type, {}).get("specificity", 0.0))

    @property
    def reaction_weight(self) -> float:
        return max(self.niche.reaction_weight, CONTENT_TYPES.get(self.content_type, {}).get("reaction_weight", 0.0))

    @property
    def terms(self) -> list[str]:
        return list(dict.fromkeys(self.niche.terms + self.creator.vocabulary))

    @property
    def avoid_terms(self) -> list[str]:
        return list(dict.fromkeys(self.niche.avoid_terms + self.creator.avoid_terms))

    def whisper_prompt(self, limit: int = 40) -> str:
        """Vocabulary hint for the speech model (names, products, slang); empty when there is none."""
        words = [t for t in self.terms if len(t) > 2][:limit]
        return ", ".join(words)

    def label(self) -> str:
        return f"{self.niche.key} v{self.niche.version}" + (f" · {self.creator_name}" if self.creator_name else "")

    def assess(self, text: str, base_score: float, duration: float) -> dict:
        """Quality and rejection assessment of one candidate's transcript text.

        Returns {"quality": 0..100, "reasons": [...], "reject": [...], "flags": [...]}. A candidate with any
        `reject` entry, or a quality below the niche's min_quality, is not rendered (its reasons are kept in
        the recording's selection report). `flags` never reject: they become review flags on the clip."""
        low = text.lower()
        words = max(1, len(re.findall(r"[a-z']+", low)))
        reasons, reject, flags = [], [], []
        adj = 0.0
        hits = [t for t in self.terms if re.search(r"\b" + re.escape(t.lower()) + r"\b", low)]
        if hits:
            adj += min(0.12, 0.03 * len(hits))
            reasons.append("niche vocabulary: " + ", ".join(hits[:5]))
        bad = [t for t in self.avoid_terms if re.search(r"\b" + re.escape(t.lower()) + r"\b", low)]
        if bad:
            adj -= min(0.15, 0.05 * len(bad))
            reasons.append("contains terms this niche avoids: " + ", ".join(bad[:3]))
        specifics = len(SPECIFIC.findall(text))
        if self.specificity_weight:
            dens = min(1.0, specifics / max(1.0, words / 40))
            adj += self.specificity_weight * (dens - 0.4)
            if dens >= 0.6:
                reasons.append("specific, checkable detail")
        reactions = [m.start() for m in REACTION.finditer(text)]
        if self.reaction_weight:
            if reactions:
                late = sum(1 for p in reactions if p >= len(text) * 0.4)
                adj += self.reaction_weight * (1.0 if late else 0.4)
                reasons.append("contains the reaction/payoff" if late else "reaction only at the start")
        rules = self.reject_rules
        if "generic_motivation" in rules and len(MOTIVATIONAL.findall(text)) >= 2 and specifics < 2:
            reject.append(REJECT_RULES["generic_motivation"])
        if "no_specifics" in rules and specifics == 0 and duration >= 20:
            reject.append(REJECT_RULES["no_specifics"])
        if "no_payoff" in rules and (not reactions or max(reactions) < len(text) * 0.4):
            reject.append(REJECT_RULES["no_payoff"])
        if "outrage_bait" in rules and len(INFLAMMATORY.findall(text)) >= max(3, words // 60):
            reject.append(REJECT_RULES["outrage_bait"])
        if "too_dependent" in rules and DEPENDENT_START.match(text.strip()) and words < 45:
            reject.append(REJECT_RULES["too_dependent"])
        frules = set(self.niche.flag_rules)
        if "financial_promotion" in frules and FIN_PROMO.search(text):
            flags.append({"category": "financial_promotion", "term": FIN_PROMO.search(text).group(0),
                          "note": FLAG_RULES["financial_promotion"]})
        if "unverifiable_claim" in frules and CLAIM.search(text):
            flags.append({"category": "claims_review", "term": CLAIM.search(text).group(0),
                          "note": FLAG_RULES["unverifiable_claim"]})
        if "spoiler_risk" in frules and SPOILER.search(text):
            flags.append({"category": "spoiler_risk", "term": SPOILER.search(text).group(0), "note": FLAG_RULES["spoiler_risk"]})
        if "sensitive_scene" in frules and SENSITIVE_SCENE.search(text):
            flags.append({"category": "sensitive_scene", "term": SENSITIVE_SCENE.search(text).group(0),
                          "note": FLAG_RULES["sensitive_scene"]})
        if "political_context" in frules:
            flags.append({"category": "political_context", "term": "", "note": FLAG_RULES["political_context"]})
        if self.niche.compliance_review and not any(f["category"] == "compliance_review" for f in flags):
            flags.append({"category": "compliance_review", "term": "",
                          "note": f"{self.niche.name}: compliance review required before any publication"})
        quality = max(0, min(100, round((base_score + adj) * 100 / 0.75)))
        if quality < self.niche.min_quality and not reject:
            reject.append(f"Quality {quality} is below this niche's minimum of {self.niche.min_quality}")
        return {"quality": quality, "reasons": reasons, "reject": reject, "flags": flags,
                "policy": self.label()}


# ---- storage helpers (SQLite tables `niches`, `niche_history`) ----------------------------

def seed_presets(db) -> int:
    """Insert presets that are not yet in the database (never overwrites owner edits)."""
    n = 0
    for key, p in PRESETS.items():
        if not db.one("SELECT key FROM niches WHERE key=?", (key,)):
            db.insert("niches", key=key, name=p.name, grp=p.group, risk=p.risk, version=p.version,
                      active=int(p.active), config_json=json.dumps(p.to_dict()))
            db.insert("niche_history", key=key, version=p.version, config_json=json.dumps(p.to_dict()),
                      note="preset")
            n += 1
    _add_review_flags(db, "gaming_streams", ["spoiler_risk", "sensitive_scene"])
    _migrate_to_context_complete(db)
    return n


def _migrate_to_context_complete(db) -> None:
    """One-time (session 9, owner-approved): niches still on the v4/v5 selector move to v6 (context-complete). Runs once;
    a niche the owner later sets back to v4/v5 stays as they chose. The change is recorded in each niche's history."""
    if db.get_kv("selection_v6_migrated"):
        return
    for r in db.query("SELECT key, version, config_json FROM niches"):
        cfg = json.loads(r["config_json"])
        if cfg.get("selection_profile") in ("v4", "v5"):
            old = cfg["selection_profile"]
            cfg["selection_profile"] = "v6"
            db.execute("UPDATE niches SET config_json=? WHERE key=?", (json.dumps(cfg), r["key"]))
            db.insert("niche_history", key=r["key"], version=r["version"], config_json=json.dumps(cfg),
                      note=f"selector {old} -> v6 (context-complete selection, session 9)")
    db.set_kv("selection_v6_migrated", "1")


def _add_review_flags(db, key: str, rules: list[str]) -> None:
    """Additive and safe: a niche already in the database gains new REVIEW flags (never rejections, never removed or
    reordered, owner edits otherwise untouched), so existing installs get them without a profile reset."""
    row = db.one("SELECT config_json FROM niches WHERE key=?", (key,))
    if not row:
        return
    cfg = json.loads(row["config_json"])
    have = list(cfg.get("flag_rules") or [])
    add = [r for r in rules if r not in have]
    if add:
        cfg["flag_rules"] = have + add
        db.execute("UPDATE niches SET config_json=? WHERE key=?", (json.dumps(cfg), key))


def load_niche(db, key: str | None) -> NicheProfile | None:
    if not key:
        return None
    row = db.one("SELECT * FROM niches WHERE key=?", (key,)) if db else None
    if row:
        p = NicheProfile.from_dict(json.loads(row["config_json"]))
        p.active, p.version = bool(row["active"]), row["version"]
        return p
    return PRESETS.get(key)


def save_niche(db, p: NicheProfile, note: str = "edited") -> NicheProfile:
    """Store an edited profile as a new version (history kept, so a change can be undone)."""
    row = db.one("SELECT version FROM niches WHERE key=?", (p.key,))
    p.version = (row["version"] + 1) if row else max(1, p.version)
    data = json.dumps(p.to_dict())
    if row:
        db.execute("UPDATE niches SET name=?, grp=?, risk=?, version=?, active=?, config_json=?, updated_at=? "
                   "WHERE key=?", (p.name, p.group, p.risk, p.version, int(p.active), data, __import__("time").time(),
                                   p.key))
    else:
        db.insert("niches", key=p.key, name=p.name, grp=p.group, risk=p.risk, version=p.version,
                  active=int(p.active), config_json=data)
    db.insert("niche_history", key=p.key, version=p.version, config_json=data, note=note)
    return p


def policy_for(db, niche_key: str | None, creator_row=None) -> EditorialPolicy | None:
    """The combined policy for a recording: its niche (or the creator's first niche) + creator profile."""
    creator = CreatorProfile.from_dict(json.loads(creator_row["profile_json"] or "{}")) if creator_row else CreatorProfile()
    niche = load_niche(db, niche_key)
    if not niche and creator_row:
        # research categories can be finer than the editorial profiles: use the first niche that has one
        for key in json.loads(creator_row["niches_json"] or "[]"):
            niche = load_niche(db, key)
            if niche:
                break
    if not niche:
        return None
    return EditorialPolicy(niche=niche, creator=creator, creator_name=creator_row["name"] if creator_row else "")
