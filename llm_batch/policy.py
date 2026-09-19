"""Per-prompt tool policy: web search / MCP / skills.

Three layers decide, in priority order (highest first):

  1. DENY list   — hard "never" constraints set by the client,
                   e.g. denied = "websearch, mcp:github, skill:pdf"
  2. @@DIRECTIVES — per-prompt overrides written inside the prompt file,
                   e.g. "@@websearch: off"
  3. RUN POLICY  — auto / always / never, set in the GUI or CLI

Directive syntax (one per line, usually at the top of a prompt; stripped
from the text before it is sent to the LLM):

    @@websearch: on | off
    @@mcp: on | off
    @@mcp: use: github, filesystem
    @@skill: pdf
    @@model: GPT-4o                 (run one prompt with a specific model)
    @@no: websearch, mcp:github        (same as the deny list, per prompt)

When the web-search policy is "auto", the tool scans the prompt for
real-time / lookup intent ("search the web", "latest", "today's price"…)
and turns web search ON only for those prompts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Conservative real-time / lookup intent patterns (matched on the lowercased prompt)
WEB_HINTS = [
    r"\bsearch (?:the )?web\b", r"\bweb ?search\b", r"\blook ?up\b",
    r"\bfind (?:it |this |that )?out\b", r"\blatest\b", r"\bnewest\b",
    r"\bcurrent(?:ly)?\s+(?:price|rate|status|score|weather|stock|version|time)\b",
    r"\bstock price\b", r"\bshare price\b", r"\bexchange rate\b",
    r"\bweather (?:today|now|tomorrow|this week)\b", r"\btoday['’]s\b",
    r"\bthis (?:week|month|year|season)\b", r"\bup[- ]to[- ]date\b",
    r"\breal[- ]time\b", r"\bwho won\b", r"\bbreaking news\b",
    r"\bnews (?:about|on|regarding|of)\b", r"\bwhat['’]s new\b",
    r"\bprice of\b.*\b(?:now|today|currently)\b",
]


def _split_csv(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def detect_web_intent(prompt: str) -> bool:
    p = (prompt or "").lower()
    return any(re.search(h, p) for h in WEB_HINTS)


DIRECTIVE_RE = re.compile(r"^\s*@@\s*([A-Za-z_]+)\s*[:=]?\s*(.*?)\s*$")


def parse_directives(prompt: str) -> tuple[str, dict, list[str]]:
    """Return (clean_prompt, directives, warnings). Directive lines are removed."""
    clean_lines: list[str] = []
    d: dict = {}
    warnings: list[str] = []
    for ln in (prompt or "").splitlines():
        m = DIRECTIVE_RE.match(ln)
        if not m:
            clean_lines.append(ln)
            continue
        key, val = m.group(1).lower(), m.group(2).strip().lower()
        if key == "model":
            d["model"] = m.group(2).strip()  # keep the user's casing
        elif key == "image":
            d["image"] = m.group(2).strip()  # filename/path, keep as typed
        elif key == "websearch" and val in ("on", "off"):
            d["websearch"] = val
        elif key == "mcp" and val in ("on", "off"):
            d["mcp"] = val
        elif key == "mcp" and val.startswith("use:"):
            d["mcp_use"] = _split_csv(val[4:])
        elif key == "skill":
            d["skill"] = _split_csv(val)
        elif key in ("no", "tools"):
            d.setdefault("no", []).extend(_split_csv(val))
        else:
            warnings.append(f"unknown directive ignored: {ln.strip()}")
    return "\n".join(clean_lines).strip(), d, warnings


@dataclass
class ToolPolicy:
    """Run-level policy (set once in the GUI / CLI)."""
    web_search: str = "auto"                # auto | always | never
    mcp: str = "never"                      # never | auto | always
    mcp_servers: dict = field(default_factory=dict)   # name -> {command,args,env,url}
    allowed_mcp: str = ""                   # "github,filesystem" (empty = all configured)
    denied: str = ""                        # "websearch, mcp, mcp:github, skill:pdf"
    skills: str = ""                        # "pdf,excel"
    model: str = ""                         # "GPT-4o", "Opus", "Qwen3-Max"… ("" = app default)
    inject_instructions: bool = True        # append a "tool settings" line to each prompt
    use_directives: bool = True             # honor @@ lines inside the prompt file

    def selected_servers(self) -> dict:
        """The MCP servers allowed by the allow-list (empty list = all)."""
        servers = dict(self.mcp_servers or {})
        allowed = _split_csv(self.allowed_mcp)
        if allowed:
            servers = {k: v for k, v in servers.items() if k in allowed}
        return servers


@dataclass
class PromptDecision:
    """What the policy resolved to for ONE prompt."""
    web_search: bool = False
    mcp: bool = False
    mcp_names: list = field(default_factory=list)
    skills: list = field(default_factory=list)
    target_model: str = ""
    force_off_web: bool = False             # explicitly told OFF (policy/deny/directive)
    force_off_mcp: bool = False
    instructions: list = field(default_factory=list)
    clean_prompt: str = ""

    def summary(self) -> str:
        parts = [f"web={'ON' if self.web_search else 'off'}"]
        if self.mcp:
            parts.append("mcp=ON(" + ",".join(self.mcp_names) + ")")
        else:
            parts.append("mcp=off")
        if self.skills:
            parts.append("skills=" + ",".join(self.skills))
        if self.target_model:
            parts.append("model=" + self.target_model)
        return "  ".join(parts)


def decide(policy: ToolPolicy, prompt: str, log=print) -> PromptDecision:
    """Resolve web search / MCP / skills for one prompt."""
    d = PromptDecision()
    directives: dict = {}
    warnings: list[str] = []

    if policy.use_directives:
        clean, directives, warnings = parse_directives(prompt)
        d.clean_prompt = clean or prompt.strip()
    else:
        d.clean_prompt = (prompt or "").strip()
    for w in warnings:
        log(f"  (directive) {w}")

    denied_all = [x.lower() for x in _split_csv(policy.denied)] + \
                 [x.lower() for x in directives.get("no", [])]

    # ---------------- web search ---------------- #
    if policy.web_search == "always":
        ws = True
    elif policy.web_search == "never":
        ws = False
        d.force_off_web = True
    else:  # auto
        ws = detect_web_intent(d.clean_prompt)

    if directives.get("websearch") == "on":
        ws = True
    if directives.get("websearch") == "off":
        ws = False
        d.force_off_web = True
    if "websearch" in denied_all:
        ws = False
        d.force_off_web = True
    d.web_search = ws

    # ---------------- MCP ---------------- #
    configured = list((policy.mcp_servers or {}).keys())
    if policy.mcp == "always":
        mcp = True
    elif policy.mcp == "never":
        mcp = False
        d.force_off_mcp = True
    else:  # auto: only when the prompt mentions MCP or a configured server name
        p_low = d.clean_prompt.lower()
        mcp = bool(re.search(r"\bmcp\b", p_low)) or any(
            n.lower() in p_low for n in configured)

    names = policy.selected_servers().keys()
    names = list(dict.fromkeys(names))  # keep order, dedupe
    if mcp and directives.get("mcp_use"):
        wanted = directives["mcp_use"]
        known = {n.lower() for n in configured}
        names = [n for n in configured if n.lower() in wanted]
        for w in wanted:
            if w.lower() not in known:
                log(f"  (directive) unknown MCP server in prompt: {w}")
    if directives.get("mcp") == "on":
        mcp = True
    if directives.get("mcp") == "off":
        mcp = False
        d.force_off_mcp = True
    for item in denied_all:
        if item == "mcp":
            mcp = False
            d.force_off_mcp = True
        elif item.startswith("mcp:"):
            nm = item[4:].lower()
            names = [n for n in names if n.lower() != nm]
    if mcp and not names:
        mcp = False  # enabled but nothing available to use
    d.mcp = mcp
    d.mcp_names = names

    # ---------------- skills ---------------- #
    skills = _split_csv(policy.skills)
    if directives.get("skill"):
        for s in directives["skill"]:
            if s not in skills:
                skills.append(s)
    for item in denied_all:
        if item.startswith("skill:"):
            nm = item[6:].lower()
            skills = [s for s in skills if s.lower() != nm]
    d.skills = skills

    # ---------------- model ---------------- #
    d.target_model = (directives.get("model", "").strip() or (policy.model or "").strip())

    # ---------------- image ---------------- #
    d.image = directives.get("image", "").strip()   # raw value; engine resolves it

    # ---------------- instruction block ---------------- #
    if policy.inject_instructions:
        inst: list[str] = []
        if d.web_search:
            inst.append("Please use web search / live browsing to check the latest "
                        "real-world information for this task.")
        elif d.force_off_web:
            inst.append("Please do NOT use web search or browsing tools; answer only "
                        "from your own knowledge.")
        if d.mcp:
            inst.append("Please use the connected MCP tools for this task: "
                        + ", ".join(d.mcp_names) + ".")
        elif d.force_off_mcp:
            inst.append("Please do NOT use any MCP tools for this task.")
        if d.skills:
            inst.append("Please use the skill(s) for this task: " + ", ".join(d.skills) + ".")
        d.instructions = inst

    return d
