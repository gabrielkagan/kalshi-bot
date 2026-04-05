---
status: active
updated: 2026-04-04
tags: [meta, dataview, dashboard]
---
# KB Dashboard (Dataview Queries)

These queries work in Obsidian with the Dataview plugin installed. They provide dynamic views across the knowledge base.

---

## Active Strategies

```dataview
TABLE status, updated, tags
FROM "kb/strategies"
WHERE status = "active"
SORT updated DESC
```

## Recent Failures (Unresolved)

```dataview
TABLE severity, updated, tags
FROM "kb/failures"
WHERE status = "active"
SORT severity ASC
```

## All Failures by Severity

```dataview
TABLE severity, status, updated
FROM "kb/failures"
SORT choice(severity, "critical", 1, "major", 2, "minor", 3) ASC
```

## Pending Decisions

```dataview
TABLE date, status, tags
FROM "kb/decisions"
WHERE status = "pending"
SORT date DESC
```

## All Decisions (Chronological)

```dataview
TABLE date, status, tags
FROM "kb/decisions"
SORT date DESC
```

## Recently Updated Articles (Last 2 Weeks)

```dataview
TABLE status, tags
FROM "kb"
WHERE updated >= date("2026-03-21")
SORT updated DESC
```

## Research Articles by Status

```dataview
TABLE status, updated, tags
FROM "kb-research"
SORT updated DESC
```

## Articles Tagged with "sol"

```dataview
TABLE status, updated
FROM "kb"
WHERE contains(tags, "sol")
SORT updated DESC
```

## Critical Failures (Resolved and Active)

```dataview
TABLE status, updated
FROM "kb/failures"
WHERE severity = "critical"
SORT updated DESC
```

## Articles by Category

```dataview
TABLE length(rows) as Count
FROM "kb"
GROUP BY file.folder
```
