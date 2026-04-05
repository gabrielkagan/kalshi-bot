---
status: active
updated: 2026-03-15
tags: [research, fraud, vendor, felix-pago]
---
# Fraud Orchestration Vendor Analysis — Complete Research

Source: Extended multi-turn research session Mar 2026
Chat link: https://claude.ai/chat/ca27ec97-8986-433e-9d6d-97cef69bbc66
Context: Felix Pago fraud strategy architecture — building toward "best fraud platform in the world"

---

## Vendors Evaluated

### Pure Orchestration Layer (the "router")

**Alloy** — Started as KYC/identity verification, grown into flexible risk decisioning platform combining fraud prevention, compliance, and onboarding orchestration. Pre-integrated API hub connecting to dozens of data providers. Strong for onboarding + transaction decisioning in one place. Very popular with fintechs and banks. Real bank customers (Mountain America, Live Oak, Ramp).

**Dodgeball** — Fraud journey orchestration that lets you deploy and integrate third-party solutions with drag-and-drop logic branches. More focused on swapping vendors in/out without re-engineering. Good for A/B testing fraud tools.

**Camunda 8** — Not fraud-specific — general-purpose process orchestration engine (BPMN/DMN). Total control over workflow design with visual BPMN diagrams. DMN tables for complex rule logic. Vendor-agnostic by design. Handles async well (manual review queues, step-up auth, waiting for callbacks). Downside: you're building fraud semantics yourself. No pre-built typology coverage, no consortium data, no device fingerprinting.

### All-in-One Platforms

**Sardine** — API-based fraud and compliance targeting fintechs, neobanks, payment companies. Core differentiator: behavioral biometrics and device-level signals. Behavioral analytics, device intelligence, identity signals, transaction monitoring. SardineX consortium data. Great for crypto/payments.

**Oscilar** — AI-native risk decisioning unifying fraud, credit, and compliance. Founded by Neha Narkhede (co-creator of Apache Kafka). Sub-100ms latency, foundational tuned models, natural-language rule creation, backtesting/A/B testing built in. 80+ pre-built integrations. SoFi is a customer. Won Best Joint AML/Fraud Innovation (Datos Insights) and Best Anti-Fraud/AML Solution (Finovate 2025). Customers seeing 50% reductions in risk rates, 75% less time on manual reviews. Pleo chose it for advanced detection with intuitive interface. Risk: relatively young (founded ~2020, Series B early 2025).

**Taktile** — Purpose-built for financial decisioning (credit, fraud, compliance). Visual decision flow builder. Strong credit customers: Zilch, Capchase, Credix, Branch. Has some orchestration but less powerful than Camunda for complex workflows.

**DataVisor** — Best for large financial institutions. Patented unsupervised ML for coordinated fraud patterns and fraud rings. End-to-end fraud + AML. Enterprise-oriented.

**Feedzai** — Enterprise fraud using supervised ML and rules orchestration. Big bank staple. Battle-hardened at massive transaction volumes. Has orchestration ambitions of its own.

**Sift** — More merchant/marketplace oriented. Good ML, solid console, less fintech-native.

**Socure** — Best-in-class identity verification. Document verification, synthetic identity detection, KYC.

## Where Each Vendor Fails

### Oscilar Fails On Credit
When Felix launches lending: need deep underwriting policy experimentation, champion/challenger testing on credit models, collections workflow optimization, credit limit automation. Oscilar came to credit from fraud — can do basic scoring but doesn't have depth of tooling or proven lending customer base. Taktile has Zilch, Capchase, Credix, Branch all running production credit decisioning.

### Taktile Fails On Fraud Network Detection
Remittance and P2P fraud are fundamentally about networks — mule chains, fraud rings, collusion between seemingly unrelated accounts. Taktile has no native graph analysis or entity link resolution. It's a decisioning engine that scores individual transactions/applicants. Can't see that account A sent to B who sent to C who all share a device fingerprint from six months ago. Oscilar has that graph layer. For a remittance company, this isn't optional.

### Both Fail On Operational Lifecycle Management
Multi-week dispute lifecycles, cross-team case routing with SLA management, long-running stateful processes spanning fraud → compliance → legal → customer support. Neither platform designed for this.

## Where Camunda Beats Oscilar and Taktile (with practical examples)

**1. Long-running stateful processes:**
A customer disputes a $50K wire transfer. Needs legal review → triggers customer communication → waits 72 hours for response → routes back to fraud ops for final disposition. Oscilar/Taktile handle the scoring; they're not designed for multi-day, multi-team processes with SLA timers, deadline escalations, and conditional branching based on external system responses.

**2. Cross-system orchestration:**
Fraud decision triggers a hold in core banking/ledger, simultaneously fires compliance alert, sends Slack notification to fraud team, kicks off customer notification flow, logs to audit trail. Camunda treats each as service tasks in a single visual BPMN diagram. In Oscilar/Taktile you'd stitch this with webhooks and custom code.

**3. Human task management:**
Camunda has native task lists with assignment rules, claim/unclaim, delegation, SLA tracking, priority queues. Oscilar has case management but it's fraud-investigation-focused. Camunda's human task layer is more general and powerful when multiple teams (fraud, compliance, legal, support) all touch the same case.

**4. Process versioning and migration:**
Deploy new BPMN process version and migrate in-flight instances from old to new. Matters with hundreds of active cases where you need to change workflow logic without breaking anything.

**5. BPMN/DMN standards:**
Workflow definitions are portable. If you move off Camunda, process models aren't locked in proprietary format.

**6. Audit and compliance:**
Process history gives complete, timestamped, visual audit trail of every decision, action, system call. Regulators love BPMN diagrams because they can actually read them.

## Recommended Five-Vendor Architecture

Given Felix's trajectory (remittance now + credit + P2P + wallet coming):

### 1. Camunda 8 — Process Orchestration Backbone
Don't wait until P2P to adopt it — instrument remittance flows now so you're not retrofitting later.

### 2. Oscilar — Primary Fraud Decisioning Engine
Graph analysis, ATO detection, ACH/card/transaction fraud models, AI agents for investigation. Strongest fit for remittance, extends well into wallet and P2P fraud scoring.

### 3. Taktile — Credit Decisioning Engine
When credit launches, run underwriting, limit management, collections through Taktile. Don't make Oscilar do credit at depth.

### 4. Sardine — Signal Enrichment Layer Across Everything
Behavioral biometrics for P2P scam detection, device intelligence for ATO prevention, SardineX consortium data. Makes Oscilar's models smarter.

### 5. Socure — Identity Verification at Onboarding Across All Products

Each vendor owns a clearly scoped layer with minimal overlap. Architecture scales as Felix adds product lines without rearchitecting.

## Complete Data Flow

```
Customer action → Camunda
  → Camunda calls Sardine (device/behavioral signals)
  → Camunda calls Socure (identity, if onboarding)
  → Camunda passes all signals to Oscilar (decisioning)
  → Oscilar returns approve/deny/review + score + explanation
  → Camunda routes based on outcome:
      Approve → proceed, log
      Deny → block, notify customer, log
      Review → assign to fraud analyst queue (Oscilar case mgmt)
        → if escalation needed → Camunda routes to compliance/legal
        → SLA timer starts
        → resolution flows back through Camunda
```

## Why This Combo Is Hard to Beat
- Camunda: total ownership of process layer, no vendor lock-in
- Oscilar: best-in-class AI decisioning with sub-100ms latency, models tune to Felix's specific patterns
- Sardine: signal depth (behavioral biometrics, consortium intel) genuinely proprietary
- Socure: IDV proven at scale with major banks
- Each vendor does what it's best at with minimal overlap
- Whole thing auditable, versioned, regulator-friendly

Main risk: integration complexity — maintaining four vendor integrations plus Camunda. Real engineering overhead. But if Felix has fraud engineering capacity, this stack is genuinely elite.

## The Strategic Wildcard

The real edge isn't the vendor stack — it's the feedback loop. Best fraud platforms (Stripe, PayPal, Square internally) win because they have tight loops between fraud outcomes, model retraining, and rule adjustment. Whichever vendor combo you pick, what makes it "best in the world" is how fast Felix can go from "we saw a new fraud pattern" to "we deployed a countermeasure."

Using Claude for autonomous fraud strategy creation — an AI layer monitoring patterns and proposing rule/model changes for human review, sitting on top of whatever vendor stack — is the kind of thing that turns a good platform into an unfair advantage.

## Sequencing Question
Not all five needed on day one. Depends on timeline for credit and P2P launches.

## Related (KB operational articles)
- (No direct kb/ counterpart)
