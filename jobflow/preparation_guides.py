"""Read-only preparation suggestions from recorded role context.

Guides are available before an application or interview exists. They do not
create tasks, reserve capacity, or establish an employer's interview process.
"""

import copy
import re

from .preparation import (AGENT_HARNESS_BANK, GUIDES_VERIFIED_ON,
                          OFFICIAL_GUIDES, PRACTICE_BANK)
from .practice_resources import (LEETCODE_PROBLEMS, PROBLEM_REASONS,
                                 PROBLEMS_BY_DOMAIN, VERIFIED_ON)


_PRACTICE = {item[0]: item for item in PRACTICE_BANK + AGENT_HARNESS_BANK}
_FOCUSES = (
    ("network", "Network infrastructure and safe orchestration",
     ("network", "networking", "tcp/ip", "bgp", "evpn", "vxlan", "smartnic",
      "smartnics", "dpu", "dpus", "routing")),
    ("agents", "Agent and harness engineering",
     ("agent", "agents", "agentic", "harness", "tool calling", "tool-calling")),
    ("storage", "Distributed systems and storage",
     ("storage", "database", "databases", "distributed systems", "replication",
      "consensus", "rdma")),
    ("platform", "Platform reliability and observability",
     ("platform", "infrastructure", "reliability", "observability", "opentelemetry",
      "kubernetes", "gke", "sre")),
    ("security", "Security and service boundaries",
     ("security", "authentication", "authorization", "identity")),
    ("frontend", "Frontend and user-facing systems",
     ("frontend", "front-end", "front end", "javascript", "typescript", "react",
      "user interface")),
)


def _mentions(text, terms):
    return any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text)
               for term in terms)


def _role_evidence(job, terms):
    """Keep short, exact recorded context separate from the inferred relevance."""
    result = []
    url = str(job.get("url") or "")
    title = str(job.get("title") or "")
    if title:
        result.append({"field": "title", "text": title, "url": url})
    description = str(job.get("description") or "")
    for part in re.split(r"\n+|(?<=[.!?])\s+", description):
        part = part.strip()
        if part and _mentions(part.casefold(), terms):
            snippet = part if len(part) <= 220 else part[:217].rsplit(" ", 1)[0] + "…"
            result.append({"field": "description", "text": snippet, "url": url})
            break
    skills = job.get("matched_skills") or []
    if isinstance(skills, str):
        skills = [skills]
    matched = [str(skill) for skill in skills if _mentions(str(skill).casefold(), terms)]
    if matched:
        result.append({"field": "matched_skills", "text": ", ".join(matched[:5]), "url": url})
    return result


_STEP_REASONS = {
    "role_research": "Start with the recorded responsibilities so preparation targets this role rather than assuming a company-wide interview format. Turn unclear requirements into questions for the recruiter or team.",
    "cache": "A bounded cache makes data-structure choices, invariants, complexity, and tests visible in a small exercise. These are transferable software-engineering foundations, not a prediction of a company question.",
    "workers": "A bounded worker pool tests concurrency, cancellation, overload, and failure propagation. Those skills support reliable infrastructure or storage services under partial failure.",
    "grpc": "Resilient service design connects infrastructure reliability to deadlines, retries, backpressure, and idempotency. Explaining a lost reply tests whether the design remains correct under failure.",
    "storage": "Replication, recovery, and consistency tradeoffs directly exercise the distributed storage focus found in the recorded role context.",
    "api": "A safe API migration tests contracts, compatibility, authorization boundaries, and rollout decisions that apply across software services.",
    "client_design": "The role's frontend focus calls for reasoning about client state, stale responses, accessibility, and performance, beyond solving an isolated algorithm.",
    "harness_loop": "An agent-focused role benefits from building the model/tool control loop and validating its stopping conditions. Fake tools isolate engineering behavior from model quality or API spending.",
    "harness_runner": "A bounded tool runner makes argument validation, cancellation, duplicate effects, and error handling testable. Those concerns follow from the recorded agent/harness specialty.",
    "harness_permissions": "Tool execution introduces trust and permission boundaries. Tracing a malicious tool result tests whether the harness limits authority rather than treating retrieved text as instructions.",
    "rate_limit": "Network infrastructure needs predictable behavior under load. A small limiter exercises time boundaries, burst handling, shared state, and concurrent requests before a larger service design.",
    "network_rollout": "The network focus makes safe changes and partial failures central design topics. A rollout plan connects topology, validation, operational risk, and recovery rather than assuming all devices change together.",
    "network_lab": "A fake-device prototype turns network orchestration ideas into observable behavior. Test one failed update and rollback in a small milestone before adding real vendor or hardware integrations.",
    "project_story": "The role needs evidence of how you make and communicate engineering decisions. A real project connects your contribution and tradeoffs to the requirements without inventing experience.",
    "review": "Re-solving a missed invariant or failure case checks retention and communication. Use your own practice notes to choose the weakness; the app does not assume which problems you have solved.",
}

_STEP_TERMS = {
    "role_research": ("responsibilities", "requirements", "design", "build"),
    "workers": ("concurrency", "reliability", "distributed", "performance"),
    "grpc": ("grpc", "distributed", "reliability", "api"),
    "network_rollout": ("orchestration", "validation", "operational safety", "configuration management"),
    "network_lab": ("automation", "automated", "testing", "vendor"),
    "project_story": ("technical direction", "architectural decisions", "lead", "mentor", "communication", "influence", "rfc"),
}


def build_role_guide(job, config, research_records=()):
    """Return suggestions, dated process evidence, and scoped recorded research.

    Suggestions are original practice, not reported questions. Durations are
    individual suggested blocks, not an additional daily schedule. Existing
    tasks, user notes and completion history are never changed.
    """
    job_id = str(job.get("id") or job.get("job_id") or "")
    company = str(job.get("company") or "Unknown company")
    title = str(job.get("title") or "Software engineer")
    skills = job.get("matched_skills") or []
    if isinstance(skills, str):
        skills = [skills]
    context = " ".join([title, str(job.get("description") or "")] +
                       [str(skill) for skill in skills]).casefold()
    matches = [(key, label) for key, label, terms in _FOCUSES if _mentions(context, terms)]
    domains = {key for key, label in matches}
    # Titles describe the role's specialty more directly than long descriptions,
    # which often contain company-wide product or mission boilerplate.
    title_domains = {key for key, label, terms in _FOCUSES if _mentions(title.casefold(), terms)}
    practice_domains = title_domains or domains
    focus = [label for key, label in matches] or ["Core software engineering"]
    primary = next((key for key, label, terms in _FOCUSES if key in practice_domains), "general")
    primary_label = next((label for key, label, terms in _FOCUSES if key == primary),
                         "core software engineering")
    primary_terms = next((terms for key, label, terms in _FOCUSES if key == primary),
                         ("software", "engineering", "systems", "testing"))
    role_evidence = _role_evidence(job, primary_terms)
    basis = ("The recorded title points to " if title_domains else
             "The recorded description and skill overlap point to ") + primary_label.lower() + ". "
    if primary == "general":
        basis = "No narrower specialty was identified in the recorded role, so this starts with software-engineering fundamentals. "

    budget = max(0, int(config.get("preparation", {}).get("daily_minutes", 90)))
    goals = config.get("daily_goals", {})
    steps = []

    def add(key, category, minutes, step_title, prompt):
        minutes = min(minutes, budget)
        if goals.get("enabled", True) and category + "_minutes" in goals:
            minutes = min(minutes, max(0, int(goals[category + "_minutes"])))
        if minutes:
            steps.append({"key": key, "category": category, "title": step_title,
                          "minutes": minutes, "prompt": prompt,
                          "why_chosen": basis + _STEP_REASONS[key],
                          "role_evidence": _role_evidence(job, _STEP_TERMS.get(key, primary_terms))})

    def practice(key):
        item = _PRACTICE[key]
        add(*item)

    add("role_research", "research", 30, "Map the role and company expectations",
        "Read the recorded job posting and the company's public materials. Write down the "
        "role's responsibilities, required skills, and two questions about the team. Separate "
        "confirmed requirements from assumptions. Review any dated process guidance below; "
        "confirm interview rounds and permitted tools when a recruiter shares the format.")

    if primary == "network":
        practice("rate_limit")
        add("network_rollout", "system_design", 30, "Design a safe network configuration rollout",
            "Choose one configuration change. Model device dependencies and a validation gate, "
            "then plan a canary, bounded rollout, telemetry checks, and rollback after partial "
            "failure. Explain idempotency and stale state. Write a short RFC with alternatives "
            "and operational risks; distinguish confirmed role requirements from assumptions.")
        add("network_lab", "learning", 30, "Prototype one network-change validation milestone",
            "Use fake device adapters; do not change a real network. In one block, compare desired "
            "and current configuration and reject an invalid change. In a later block, simulate "
            "one failed update and verify rollback or safe retry. Save tests and explain what "
            "vendor differences or hardware-offload constraints would require further research.")
    elif primary == "agents":
        practice("harness_loop")
        practice("harness_runner")
        practice("harness_permissions")
    else:
        practice("workers" if practice_domains & {"storage", "platform"} else "cache")
        if "storage" in practice_domains:
            practice("storage")
        elif "platform" in practice_domains:
            practice("grpc")
        elif "frontend" in practice_domains:
            add("client_design", "system_design", 30, "Design a reliable interactive client",
                "Choose a small interface relevant to the recorded role. Define client state, "
                "API boundaries, loading and error behavior, accessibility, and caching. Explain "
                "how you handle stale responses, retries, performance, and automated testing.")
        else:
            practice("api")

    add("project_story", "experience", 30, "Connect a real project to the role",
        "Choose a project you can substantiate from your recorded experience. Explain the "
        "problem, your individual contribution, alternatives, tradeoffs, collaboration, and "
        "outcome. Relate one aspect to the recorded role requirements. Use only supported "
        "achievements and metrics; note gaps as learning goals rather than claiming experience.")
    practice("review")

    coding_minutes = min(30, budget)
    if goals.get("enabled", True) and "coding_minutes" in goals:
        coding_minutes = min(coding_minutes, max(0, int(goals["coding_minutes"])))
    problems = []
    if coding_minutes:
        for number in PROBLEMS_BY_DOMAIN[primary]:
            problem_title, slug, difficulty, topics = LEETCODE_PROBLEMS[number]
            url = "https://leetcode.com/problems/" + slug + "/"
            problems.append({
                "id": number, "title": number + ". " + problem_title, "url": url,
                "difficulty": difficulty, "topics": list(topics),
                "why_chosen": basis + PROBLEM_REASONS[number],
                "role_evidence": copy.deepcopy(role_evidence), "observed_at": VERIFIED_ON,
                "access": "Public problem statement verified; sign in to submit.",
                "practice_category": "coding", "minutes": coding_minutes,
                "practice_note": "Skill practice, not a reported company question.",
                "sources": [{"url": url, "title": "LeetCode: " + problem_title}],
            })

    seed = OFFICIAL_GUIDES.get(company.strip().casefold())
    process = {
        "text": (seed["text"] + " This is saved guidance, last verified " + GUIDES_VERIFIED_ON +
                 "; confirm the current process for this role." if seed else
                 "Company-specific interview format has not been verified in the saved guidance. "
                 "Use official company materials or recruiter instructions to confirm the process."),
        "sources": ([{"url": seed["url"], "title": company + " official interview guidance"}]
                    if seed else []),
        "observed_at": GUIDES_VERIFIED_ON if seed else "",
    }
    company_key = company.strip().casefold()
    research = [copy.deepcopy(row) for row in research_records
                if row.get("kind") not in ("company_location", "company_location_review") and
                ((job_id and str(row.get("job_id") or "") == job_id) or
                (not row.get("job_id") and row.get("company") and
                 str(row["company"]).strip().casefold() == company_key))]
    return {
        "job_id": job_id, "company": company, "title": title,
        "summary": "Start preparing for this role before an interview is scheduled. "
                   "Suggested focus comes from the recorded title, description, and matched skills; "
                   "work through the guide across your existing preparation blocks.",
        "focus": focus, "steps": steps, "process": process, "research": research,
        "leetcode_problems": problems,
        "leetcode_note": ("Choose one problem for a coding block; use another block for review if needed. "
                          "These public problems are chosen for transferable role skills, not employer "
                          "question frequency. Problem-level completion history has not been imported, "
                          "so the list does not assume what you have already solved." if coding_minutes else
                          "No LeetCode practice block is suggested because the configured coding or "
                          "daily preparation budget is zero. Increase that target to include coding practice."),
        "disclaimer": "These are original practice suggestions, not actual reported company questions. "
                      "The guide adds no scheduled tasks or extra daily time. Use your shared daily "
                      "preparation budget and keep confirmed interview requirements separate from suggestions.",
    }
