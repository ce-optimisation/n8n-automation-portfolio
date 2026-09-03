# 📈 Lead Management & Revenue Operations

Detailed breakdowns of production-grade marketing and sales operations automation systems.

---

### 1. Lead Intelligence – Enrich, Score, Route
*   **The Problem:** Manual lead triage results in slow response times, allowing high-value enterprise leads to grow cold while sales reps waste time on unqualified submissions.
*   **The Solution:** An automated ingestion pipeline that catches incoming webhooks, enriches data points externally, uses advanced AI logic to grade leads, and routes high-scoring prospects straight to sales.
*   **Core Architecture:**
    *   `Webhook Node`: Captures raw incoming payloads in real time.
    *   `HTTP Request (Data Enrichment)`: Pulls real-time company metrics and firmographic data via API (e.g., Apollo/Clearbit).
    *   `HTTP Request (AI Logic Engine)`: Passes enriched profiles to LLMs (Anthropic Claude) to evaluate against strict Ideal Customer Profiles (ICP) and calculate an objective 0–100 score.
    *   `IF Conditional Routing`: Directs hot leads (>= 70) straight to high-priority sales channels while routing remaining leads to automated nurture sequences.

### 2. Inquiry to Quote to Follow-up
*   **The Problem:** Delayed quotes cause prospective buyers to turn to faster competitors, while a lack of consistent follow-up allows warm pipeline inquiries to stall.
*   **The Solution:** An end-to-end quote-generation engine that instantly generates proposal documentation upon inquiry and executes multi-stage, state-aware follow-up delays until a response is detected.
*   **Core Architecture:**
    *   `HTTP Request (Document Engine)`: Synthesizes dynamic PDF quotes immediately from web form inputs.
    *   `Wait Node (State Check Loop)`: Pauses execution for custom durations (N days) between contact intervals.
    *   `HTTP Request (CRM State Sync)`: Dynamically checks current CRM opportunity states prior to follow-up to ensure booked or replied clients exit the loop early.

### 3. Dormant Lead Re-engagement
*   **The Problem:** Marketing databases lose value as older leads naturally decay without regular engagement check-ins from busy sales reps.
*   **The Solution:** A scheduled workflow that queries cold database entries, filters out recently touched accounts, fires low-pressure re-engagement sequences, and auto-archives dead opportunities.
*   **Core Architecture:**
    *   `Schedule Trigger`: Executes systematically every morning at 08:00 to crawl records.
    *   `Filter Layer`: Separates leads meeting the precise dormancy limit (>= 30 days inactive) from those that have already received re-engagement messages.
    *   `HTTP Request (Database Writer)`: Updates records instantly to flag sent messaging or change customer statuses to archived once past the maximum inactivity threshold.