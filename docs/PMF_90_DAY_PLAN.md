# Overlayr: 90-Day Product-Market-Fit Plan

The goal of this phase is not maximum feature count. It is to prove that a
specific group of players repeatedly uses Overlayr and will pay for it.

## Product thesis

Overlayr is a game-aware language layer for games that are unavailable or
poorly localized in a player's language.

The first defensible loop is:

1. A player selects a game window.
2. Overlayr remembers its language pair and capture region.
3. The player teaches preferred game terminology.
4. That game's profile becomes more useful over time.
5. With explicit consent, high-quality profiles can later be shared with the
   community and offered to publishers as localization data.

## Pick the first three games

Score candidate games from 1–5 and select the three highest totals.

| Criterion | Weight |
| --- | ---: |
| Large Thai/SEA community that can be reached directly | 3 |
| Important JP/CN/KR text is missing or poorly localized | 3 |
| Players return several times per week | 2 |
| Text appears in stable dialogue/UI regions | 2 |
| Community leaders or creators are willing to test | 2 |
| Low anti-cheat and terms-of-service risk | 3 |

Do not select three games from unrelated audiences. The initial cohort should
share a language need and discovery channel.

## Days 1–14: problem validation

- Recruit 30–50 players from the three target-game communities.
- Watch at least 15 players install and use Overlayr over screen share.
- Do not explain the interface unless the participant is completely blocked.
- Record time to first useful translation, failed OCR, setup confusion, and
  terminology corrections.
- Ask for the last occasion when language stopped or slowed their play. Avoid
  hypothetical questions such as “Would you use this?”

Interview prompts:

1. Show the last screen you could not understand.
2. What did you do instead?
3. How often does this happen in a normal week?
4. What is wrong with the translator you use now?
5. Which names or game terms must never change between screens?
6. Would losing this workflow tomorrow materially affect how you play?

Exit criterion: at least 10 participants complete a useful translation without
developer intervention, and at least 5 return during the following week.

## Days 15–45: closed alpha

- Ship one-click game-profile activation.
- Keep the first useful translation under three minutes from download.
- Use “Teach Translation” to collect preferred terminology locally.
- Publish a short setup clip for each target game.
- Recruit 300–500 testers through game-specific Discord, Reddit, Facebook,
  Steam, creator, or guild communities.
- Review the local product funnel weekly with:

```powershell
.\.venv\Scripts\python.exe .\scripts\product_metrics.py
```

Events contain counts and technical state only. OCR text, translations,
screenshots, and window titles are deliberately excluded.

Exit criteria:

- Activation: at least 60% reach a rendered translation.
- Median time to first translation: under 180 seconds.
- D7 retention: at least 30%.
- Core users: at least three capture sessions per week.
- Translation correction rate trends down for an established game profile.

## Days 46–75: willingness to pay

- Offer only Free and Pro Early Access.
- Test ฿249/month or a localized equivalent.
- Do not limit translations so aggressively that users avoid real-time mode.
- Put convenience, context, cloud sync, priority game profiles, and advanced
  correction workflows in Pro.
- Interview every churned or refunded early customer.

Exit criteria:

- At least 100 people are presented with a real purchase decision.
- At least 5% convert to paid.
- Paid users retain better than the free cohort.
- Support and translation costs leave at least 70% gross margin.

## Days 76–90: repeatable distribution

- Produce a before/after video for each supported game.
- Create an indexable landing page per game and language pair.
- Add referral attribution for creators and community leaders.
- Launch one new game profile using the same playbook without founder-led
  onboarding.
- Contact 10 small publishers with evidence of demand, not a generic SDK pitch.

Publisher evidence should include:

- Number of active players attempting to translate the game.
- Most translated UI or dialogue regions.
- Language pairs requested.
- Correction volume and stabilized glossary size.
- Retention difference between players with and without a mature profile.

## Decision gates

| Gate | Minimum evidence |
| --- | --- |
| Useful utility | 1,000 MAU and 100 paying users |
| Repeatable business | 10,000 MAU and $10k MRR |
| Scalable consumer product | 100,000 MAU and $1M+ ARR |
| Platform candidate | 1M MAU, community profile flywheel, publisher revenue |

If retention misses the target, improve the game workflow before adding more
OCR engines, pricing tiers, or enterprise infrastructure.

