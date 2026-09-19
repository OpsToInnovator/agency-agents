# Examples

This directory contains example outputs demonstrating how the agency's agents can be orchestrated together to tackle real-world tasks.

## Why This Exists

The agency-agents repo defines dozens of specialized agents across engineering, design, marketing, product, support, spatial computing, and project management. But agent definitions alone don't show what happens when you **deploy them all at once** on a single mission.

These examples answer the question: *"What does it actually look like when the full agency collaborates?"*

## Contents

### [nexus-spatial-discovery.md](./nexus-spatial-discovery.md)

**What:** A complete product discovery exercise where 8 agents worked in parallel to evaluate a software opportunity and produce a unified plan.

**The scenario:** Web research identified an opportunity at the intersection of AI agent orchestration and spatial computing. The entire agency was then deployed simultaneously to produce:

- Market validation and competitive analysis
- Technical architecture (8-service system design with full SQL schema)
- Brand strategy and visual identity
- Go-to-market and growth plan
- Customer support operations blueprint
- UX research plan with personas and journey maps
- 35-week project execution plan with 65 sprint tickets
- Spatial interface architecture specification

**Agents used:**
| Agent | Role |
|-------|------|
| Product Trend Researcher | Market validation, competitive landscape |
| Backend Architect | System architecture, data model, API design |
| Brand Guardian | Positioning, visual identity, naming |
| Growth Hacker | GTM strategy, pricing, launch plan |
| Support Responder | Support tiers, onboarding, community |
| UX Researcher | Personas, journey maps, design principles |
| Project Shepherd | Phase plan, sprints, risk register |
| XR Interface Architect | Spatial UI specification |

**Key takeaway:** All 8 agents ran in parallel and produced coherent, cross-referencing plans without coordination overhead. The output demonstrates the agency's ability to go from "find an opportunity" to "here's the full blueprint" in a single session.

### [crypto-arbitrage-bot/](./crypto-arbitrage-bot/)

**What:** A working Python bot that reproduces the viral "built a trading bot with Claude Code in 2 days, $68 → $750,000" post, and then shows you why the money part is impossible.

**The scenario:** The post claims a bot that scans 50+ markets, syncs live Binance data, spots "price errors" and executes on mispricing. This example does all of that for real: WebSocket feeds from Binance, Coinbase and Kraken (~190 markets), cross-exchange and triangular arbitrage detectors, anomaly detection, a fee-aware paper trader, a risk manager with a kill switch, and a gated live path that defaults to Binance's validation-only order/test endpoint and needs two more explicit switches before it can send real orders. It also ships a recorded tape, an offline test suite, and a README that walks through the fee schedules, the return arithmetic and the academic evidence.

**Key takeaway:** Thousands of gross-positive spreads per minute, zero or single digits net of fees, and the first "profit" it ever reported was a ticker collision (Binance `ONE` vs Kraken `ONE`). Build the scanner, learn the microstructure, keep your $68.

## Adding New Examples

If you run an interesting multi-agent exercise, consider adding it here. Good examples show:

- Multiple agents collaborating on a shared objective
- The breadth of the agency's capabilities
- Real-world applicability of the agent definitions
