---
name: refund-handoff
description: Prepare an evidence-backed refund recommendation without taking unapproved action.
tags: [client-service, handoff]
---

# Refund handoff

## When to use

Use for a customer refund request that needs a human decision.
Do not use for issuing a refund or changing account settings.

## Method

Confirm the order number first.

1. Read the request and any existing approval.
2. Inspect available connectors and the permitted browser before asking the user for screenshots.
3. Distinguish configured credentials from completed authorisation.
4. Never ask for passwords, tokens or other secrets.
5. Prepare a recommendation. Obtain explicit approval before acting.
6. Verify the result before reporting success.
