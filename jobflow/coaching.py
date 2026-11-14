"""Evidence-based daily scorecards and bounded preparation suggestions.

This module is deliberately pure: checking progress does not create activity,
change application stages, or move the user's tasks. A completed task's planned
effort is an estimate, never evidence that those minutes were spent.
"""
import copy
import re
from datetime import date, datetime, time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


DEFAULT_GOALS = {
    "applications": 10,
    "learning_minutes": 30,
    "coding_minutes": 30,
    "system_design_minutes": 30,
    "application_minutes_each": 15,
}
CATEGORIES = ("learning", "coding", "system_design")
INACTIVE_STAGES = {"rejected", "withdrawn", "offer", "accepted"}
SUBMITTED_STAGES = {"applied", "recruiter", "technical", "onsite", "offer"}
EXERCISES = {
    "learning": "Study one small course section, then build a minimal agent loop with tool validation, a step limit, and a reproducible test. Save the code and one failure you found.",
    "coding": "Solve one appropriately sized coding problem from a blank file under a timer; explain the invariant, complexity, and edge cases, then record one mistake to revisit.",
    "system_design": "Sketch an agent execution service: durable runs, tool permissions, retries, cancellation, budgets, observability, and replay. Explain one failure path and its tradeoffs.",
}


def _category(value):
    value = str(value or "").casefold().replace("-", "_").replace(" ", "_")
    return {"agent_harness": "learning", "course": "learning", "leetcode": "coding"}.get(value, value)


def _positive_minutes(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0 or str(value).strip() not in (str(number), str(float(number))):
        return None
    return number


def _timestamp(value, zone):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(zone) if parsed.tzinfo is not None else None


def _now(value, zone):
    if value is None:
        return datetime.now(zone)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=zone)
    raise ValueError("today must be an ISO date, date, or datetime")


def _label(value):
    return " ".join(str(value or "").split()).replace("|", "\\|")


def _http_url(value):
    try:
        parsed = urlparse(str(value))
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _select_resources(research, upcoming, relevant_jobs):
    """Select sourced learning notes, favoring a confirmed target over keywords."""
    job_ids = {str(row.get("job_id", row.get("id", ""))) for row in upcoming + relevant_jobs}
    companies = {str(row.get("company", "")).casefold() for row in upcoming + relevant_jobs}
    context = " ".join(str(row.get(key, "")) for row in upcoming + relevant_jobs
                       for key in ("round", "title", "description")).casefold()
    # These are the user's ongoing goals, not a claim about an employer's test.
    baseline = {"learning", "coding", "leetcode", "system_design", "agent_harness", "agents", "agent", "evaluation", "evals"}
    selected = []
    for row in research:
        if row.get("kind") != "learning_resource" or not row.get("text"):
            continue
        sources = [copy.deepcopy(source) for source in row.get("sources", [])
                   if isinstance(source, dict) and _http_url(source.get("url", ""))]
        if not sources:
            continue
        topics = row.get("topics", [])
        if not isinstance(topics, list):
            topics = []
        topics = {_category(topic) for topic in topics if isinstance(topic, str)}
        tied = bool(row.get("job_id") and str(row["job_id"]) in job_ids)
        tied = tied or bool(row.get("company") and str(row["company"]).casefold() in companies)
        overlap = any(topic and re.search(r"(?<!\w)" + re.escape(topic.replace("_", " ")) + r"(?!\w)", context)
                      for topic in topics)
        if not tied and not overlap and not topics.intersection(baseline):
            continue
        resource = copy.deepcopy(row)
        resource["sources"] = sources
        resource["relevance"] = "recorded interview or target role" if tied else "role topic" if overlap else "daily preparation goal"
        selected.append((2 if tied else 1 if overlap else 0, resource))
    selected.sort(key=lambda item: (-item[0], _positive_minutes(item[1].get("priority")) or 999,
                                    str(item[1].get("id", ""))))
    # Avoid recommending the same source through multiple imported summaries.
    urls, result = set(), []
    for _, resource in selected:
        if all(source["url"] in urls for source in resource["sources"]):
            continue
        result.append(resource)
        urls.update(source["url"] for source in resource["sources"])
        if len(result) == 7:
            break
    return result


def build_coaching(applications, tasks, activities, interviews, research, jobs, config, today=None):
    """Return a JSON-friendly daily plan and progress report without mutation.

    An activity linked by ``task_id`` is excluded when that completed task has
    an explicit ``actual_minutes`` total on the same local date. The task total
    is authoritative for that session; unrelated sessions remain additive.
    """
    prep = config.get("preparation", {})
    timezone = prep.get("timezone", config.get("schedule", {}).get("timezone", "America/Los_Angeles"))
    zone = ZoneInfo(timezone)
    now = _now(today, zone)
    day = now.date().isoformat()
    goals = dict(DEFAULT_GOALS)
    goals.update({key: value for key, value in config.get("daily_goals", {}).items() if key in goals})
    for key, value in goals.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Daily goal {} must be a nonnegative integer.".format(key))
    capacity = prep.get("daily_minutes", 90)
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
        raise ValueError("Preparation daily_minutes must be a nonnegative integer.")
    cautions = []

    submitted, undated = set(), set()
    for row in applications:
        key = str(row.get("job_id") or row.get("id") or "")
        stamp = _timestamp(row.get("applied_at"), zone)
        if key and stamp and stamp.date() == now.date():
            submitted.add(key)
        elif key and not stamp and row.get("stage") in SUBMITTED_STAGES:
            undated.add(key)
    if undated:
        cautions.append("{} submitted-stage application(s) have no valid applied_at timestamp; their submission dates are unknown and are excluded from today's count.".format(len(undated)))

    progress = {"applications": {"target": goals["applications"], "logged": len(submitted),
                                  "remaining": max(0, goals["applications"] - len(submitted)),
                                  "job_ids": sorted(submitted), "undated": len(undated)}}
    for category in CATEGORIES:
        progress[category] = {"target": goals[category + "_minutes"], "logged": 0,
                              "estimated_completed_minutes": 0, "activity_ids": [], "task_ids": []}
    task_actuals, estimated_tasks = {}, []
    uncategorized_actual_minutes = 0
    unknown_completed = 0
    for task in tasks:
        if task.get("status") != "done":
            continue
        stamp = _timestamp(task.get("completed_at"), zone)
        if not stamp:
            unknown_completed += 1
            continue
        if stamp.date() != now.date():
            continue
        actual = _positive_minutes(task.get("actual_minutes"))
        category = _category(task.get("category"))
        if actual is not None:
            task_actuals[str(task.get("id", ""))] = actual
            if category in CATEGORIES:
                progress[category]["logged"] += actual
                progress[category]["task_ids"].append(task.get("id", ""))
            else:
                uncategorized_actual_minutes += actual
        else:
            estimated = _positive_minutes(task.get("minutes")) or 0
            estimated_tasks.append({"id": task.get("id", ""), "minutes": estimated,
                                    "category": category, "title": task.get("title", "Completed task")})
            if category in CATEGORIES:
                progress[category]["estimated_completed_minutes"] += estimated
    logged_activities, excluded_activities = {}, []
    for row in activities:
        if row.get("date") != day or _category(row.get("category")) not in CATEGORIES:
            continue
        minutes = _positive_minutes(row.get("minutes"))
        key = str(row.get("id", ""))
        if minutes is None or not key:
            continue
        if str(row.get("task_id", "")) in task_actuals:
            excluded_activities.append(key)
            continue
        # Store records are unique by ID; remain safe if callers pass duplicates.
        logged_activities[key] = row
    for key, row in logged_activities.items():
        category = _category(row["category"])
        progress[category]["logged"] += _positive_minutes(row["minutes"])
        progress[category]["activity_ids"].append(key)
    for category in CATEGORIES:
        progress[category]["remaining"] = max(0, progress[category]["target"] - progress[category]["logged"])
    if excluded_activities:
        cautions.append("{} linked activity record(s) are excluded because the completed task's actual_minutes total already covers them on this date.".format(len(excluded_activities)))
    if unknown_completed:
        cautions.append("{} completed task(s) have no valid completed_at timestamp; no completion date or spent time was inferred.".format(unknown_completed))
    if uncategorized_actual_minutes:
        cautions.append("{} actual task minutes have a category outside the daily goals. Set the task category to learning, coding, or system_design where appropriate; linked activities do not override a task's actual-time total or category.".format(uncategorized_actual_minutes))

    apps = {str(row.get("job_id", "")): row for row in applications}
    upcoming = []
    for row in interviews:
        app = apps.get(str(row.get("job_id", "")), {})
        if app.get("stage") in INACTIVE_STAGES or str(row.get("status", "")).lower() in ("cancelled", "canceled", "completed"):
            continue
        stamp = _timestamp(row.get("starts_at"), zone)
        if stamp is None or stamp.timestamp() < now.timestamp():
            continue
        upcoming.append({**copy.deepcopy(row), "company": row.get("company") or app.get("company", "Unknown company"),
                         "starts_at": stamp.isoformat(), "days_away": (stamp.date() - now.date()).days})
    upcoming.sort(key=lambda row: (_timestamp(row["starts_at"], zone).timestamp(), str(row.get("id", ""))))
    upcoming = upcoming[:5]
    relevant_jobs = sorted([copy.deepcopy(row) for row in jobs
                            if row.get("eligible") and row.get("availability") != "closed"
                            and row.get("score", 0) >= config.get("search", {}).get("min_score", 0)],
                           key=lambda row: (-row.get("score", 0), str(row.get("company", "")), str(row.get("id", ""))))[:5]
    resources = _select_resources(research, upcoming, relevant_jobs)

    # The existing preparation plan is a capacity reservation, not another
    # checklist to add on top of the coach's goal suggestions.
    pending = [row for row in tasks if row.get("due_date") == day and row.get("status") in ("todo", "doing")]
    pending.sort(key=lambda row: (row.get("status") != "doing", str(row.get("id", ""))))
    logged_minutes = sum(task_actuals.values()) + sum(_positive_minutes(row["minutes"]) for row in logged_activities.values())
    estimated_completed = sum(row["minutes"] for row in estimated_tasks)
    if estimated_completed:
        cautions.append("Completed tasks have {} planned minutes without actual-time totals. Record actual_minutes or a dated session log to establish goal credit; capacity uses these estimates provisionally.".format(estimated_completed))
    linked_activity_minutes = {}
    for row in logged_activities.values():
        task_id = str(row.get("task_id", ""))
        linked_activity_minutes[task_id] = linked_activity_minutes.get(task_id, 0) + _positive_minutes(row["minutes"])
    # A planned completion estimate and an explicit session for that task are
    # two descriptions of the same work, not additive capacity reservations.
    estimated_reservation = sum(max(0, row["minutes"] - linked_activity_minutes.get(str(row["id"]), 0))
                                for row in estimated_tasks)
    remaining_capacity = capacity - logged_minutes - estimated_reservation
    blocks, planned_by_category = [], {category: 0 for category in CATEGORIES}
    for task in pending:
        planned_minutes = _positive_minutes(task.get("minutes")) or 0
        logged_task_minutes = linked_activity_minutes.get(str(task.get("id", "")), 0)
        minutes = max(0, planned_minutes - logged_task_minutes)
        category = _category(task.get("category"))
        blocks.append({"category": category, "minutes": minutes, "title": task.get("title", "Preparation task"),
                       "task_id": task.get("id", ""), "existing": True,
                       "planned_minutes": planned_minutes, "logged_minutes": logged_task_minutes})
        if category in CATEGORIES:
            planned_by_category[category] += minutes
        remaining_capacity -= minutes
    if remaining_capacity < 0:
        cautions.append("Recorded time, completed-effort estimates, and today's existing tasks exceed the {}-minute preparation budget by {} minutes. Review the unstarted tasks; no task has been moved or changed.".format(capacity, -remaining_capacity))
    remaining_capacity = max(0, remaining_capacity)
    # When the next confirmed round is close, spend available capacity on it
    # first; do not pretend the other daily targets disappeared.
    category_order = list(CATEGORIES)
    if upcoming and upcoming[0]["days_away"] <= 3:
        round_name = str(upcoming[0].get("round", "")).casefold()
        focus = "system_design" if any(term in round_name for term in ("design", "architecture")) else "coding" if any(term in round_name for term in ("coding", "technical", "screen")) else "learning"
        if any(term in round_name for term in ("agent", "harness")):
            focus = "learning"
        category_order.remove(focus)
        category_order.insert(0, focus)
    for category in category_order:
        need = max(0, progress[category]["remaining"] - planned_by_category[category] - progress[category]["estimated_completed_minutes"])
        minutes = min(need, remaining_capacity)
        if minutes:
            blocks.append({"category": category, "minutes": minutes, "title": EXERCISES[category],
                           "task_id": "", "existing": False})
            planned_by_category[category] += minutes
            remaining_capacity -= minutes
    target_minutes = sum(goals[category + "_minutes"] for category in CATEGORIES)
    if target_minutes > capacity:
        cautions.append("The preparation targets total {} minutes, above the existing {}-minute budget. The budget is unchanged; reduce a target, rotate topics, or explicitly increase capacity.".format(target_minutes, capacity))
    deferred = {category: max(0, progress[category]["remaining"] - planned_by_category[category] - progress[category]["estimated_completed_minutes"])
                for category in CATEGORIES}
    if any(deferred.values()):
        cautions.append("Today's capacity leaves these target minutes unplanned: {}. Existing task text and dates are preserved; review an unstarted task if you want to rebalance.".format(
            ", ".join("{} {}".format(category.replace("_", " "), minutes) for category, minutes in deferred.items() if minutes)))

    recommendations = [
        "Aim for {} verified, role-fit submissions. Prioritize saved roles and good matches; check live employer status before applying. Record a shortfall if fewer suitable roles are available.".format(goals["applications"]),
        "Use a course-to-build-to-test loop: learn one concept, implement it, run a small evaluation, and record a failure and the fix. Passive viewing alone is weak interview evidence.",
        "Keep an error log for coding and design practice; revisit one previous error from memory before choosing more material.",
    ]
    if upcoming:
        nearest = upcoming[0]
        recommendations.insert(0, "Prioritize the recorded {} interview at {} ({}) and confirm its tool rules. Course exercises below are generic practice, not reported employer questions.".format(
            nearest.get("round", "technical"), nearest["company"], nearest["starts_at"]))
    else:
        recommendations.insert(0, "No upcoming interview is recorded. Use the daily fundamentals plan, and add the next interview so its actual round and deadline can guide preparation.")
    if not resources:
        recommendations.append("No relevant sourced learning materials are recorded yet. Add a source URL and observation date before relying on a recommendation as researched guidance.")

    application_estimate = goals["applications"] * goals["application_minutes_each"]
    lines = ["# Daily job-search coach", "", "{} · {}".format(day, timezone), "",
             "## Progress", "", "| Goal | Logged | Target | Remaining |", "|---|---:|---:|---:|"]
    for category in ("applications",) + CATEGORIES:
        row = progress[category]
        unit = "" if category == "applications" else " min"
        lines.append("| {} | {}{} | {}{} | {}{} |".format(category.replace("_", " ").capitalize(), row["logged"], unit, row["target"], unit, row["remaining"], unit))
    lines += ["", "Logged means explicitly recorded submissions or actual minutes. A missing log means unknown progress, not proof that no work happened.",
              "Planned minutes on completed tasks are estimates and do not count toward logged time. Linked activities use a completed task's actual_minutes total when both refer to this local date.", "",
              "## Today's time blocks", "",
              "Preparation capacity: {} minutes. Application effort is separate: about {} minutes for {} submissions at an estimated {} minutes each. Full-day working estimate: {} minutes, plus breaks; application times vary.".format(
                  capacity, application_estimate, goals["applications"], goals["application_minutes_each"], capacity + application_estimate), "",
              "- Applications: {} remaining; verify fit and status, tailor the essentials, submit yourself, then record each submission.".format(progress["applications"]["remaining"])]
    for block in blocks:
        source = "existing task {}".format(block["task_id"]) if block["existing"] else "suggested generic practice"
        lines.append("- {} min · {} · {} ({})".format(block["minutes"], block["category"].replace("_", " "), _label(block["title"]), source))
    if logged_minutes or estimated_completed:
        lines.append("- Already recorded today: {} actual minutes; {} planned minutes on completed tasks without actual-time logs (estimates only).".format(logged_minutes, estimated_completed))
    lines += ["", "These blocks use the existing preparation budget. Existing task blocks show remaining planned effort after today's linked session logs; suggested practice fills only spare capacity. No task is changed by this report.", "", "## Coaching suggestions", ""]
    lines += ["- " + _label(item) for item in recommendations]
    if relevant_jobs:
        lines += ["", "## Roles informing preparation", ""]
        for row in relevant_jobs:
            lines.append("- {} · {} · score {}{}".format(_label(row.get("company")), _label(row.get("title")), row.get("score", 0), " · status needs rechecking" if row.get("availability") == "missing" else ""))
    lines += ["", "## Learning materials", ""]
    if resources:
        for row in resources:
            observed = row.get("observed_at", row.get("observed_on", "date not recorded"))
            lines.append("- {} · observed {} · relevance: {}".format(_label(row.get("title", row.get("id", "Resource"))), _label(observed), row["relevance"]))
            lines.append("  " + _label(row["text"]))
            for source in row["sources"]:
                lines.append("  - [{}]({})".format(_label(source.get("title", source["url"])).replace("[", "\\[").replace("]", "\\]"), source["url"]))
    else:
        lines.append("No relevant sourced materials are recorded.")
    if cautions:
        lines += ["", "## Items to review", ""] + ["- " + item for item in cautions]
    return {"date": day, "timezone": timezone, "goals": goals, "progress": progress,
            "preparation_capacity_minutes": capacity, "preparation_target_minutes": target_minutes,
            "logged_preparation_minutes": logged_minutes, "estimated_completed_minutes": estimated_completed,
            "estimated_application_minutes": application_estimate, "estimated_total_minutes": capacity + application_estimate,
            "blocks": blocks, "unplanned_goal_minutes": deferred, "upcoming_interviews": upcoming,
            "relevant_jobs": relevant_jobs, "resources": resources, "recommendations": recommendations,
            "cautions": cautions, "excluded_activity_ids": sorted(excluded_activities), "markdown": "\n".join(lines) + "\n"}
