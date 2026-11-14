"""Small verified catalog of public coding problems, never company questions.

Metadata was checked on the linked official problem pages. Selection and role
relevance are Jobflow's practice suggestions, not claims made by LeetCode or an
employer. Problem statements and solutions are intentionally not copied here.
"""

VERIFIED_ON = "2026-11-14"

# id, title, official slug, difficulty, conceptual practice topics
LEETCODE_PROBLEMS = {
    "146": ("LRU Cache", "lru-cache", "Medium", ["Hash maps", "Linked lists", "Bounded state"]),
    "207": ("Course Schedule", "course-schedule", "Medium", ["Directed graphs", "Cycle detection", "Dependencies"]),
    "743": ("Network Delay Time", "network-delay-time", "Medium", ["Weighted graphs", "Shortest paths", "Reachability"]),
    "933": ("Number of Recent Calls", "number-of-recent-calls", "Easy", ["Queues", "Time windows", "Boundary conditions"]),
    "347": ("Top K Frequent Elements", "top-k-frequent-elements", "Medium", ["Frequency counts", "Heaps", "Complexity"]),
    "56": ("Merge Intervals", "merge-intervals", "Medium", ["Sorting", "Intervals", "Edge cases"]),
}

PROBLEMS_BY_DOMAIN = {
    "network": ("743", "146", "933", "207"),
    "agents": ("207", "146", "933"),
    "storage": ("146", "347", "56", "207"),
    "platform": ("347", "933", "146", "207"),
    "security": ("933", "146", "207"),
    "frontend": ("56", "146", "933"),
    "general": ("146", "207", "56"),
}

PROBLEM_REASONS = {
    "146": "Practice bounded state, eviction invariants, and constant-time lookups. These are useful foundations for cache or connection-metadata design; a production connection pool also needs ownership, expiry, and concurrency rules.",
    "207": "Practice detecting cycles and reasoning about dependency order. Apply that reasoning to provisioning, rollout dependencies, or tool workflows, where a cycle can prevent progress.",
    "743": "Practice directed graph reachability and path costs, then explain unreachable nodes and complexity. This is a small model for reasoning about network topology, not an implementation of a production routing protocol.",
    "933": "Practice expiring old events and testing inclusive time-window boundaries. This easy warm-up supports accurate telemetry counters and rate-limit reasoning; extend it separately to reject requests or handle concurrency.",
    "347": "Practice summarizing frequent events and comparing time and memory costs. Relate this to telemetry heavy hitters, hot keys, or other high-volume signals in a systems role.",
    "56": "Practice ordering ranges and handling overlap and boundary cases. Relate this to batch windows, ranges of data, or client state updates, and explain which edge cases your tests cover.",
}
