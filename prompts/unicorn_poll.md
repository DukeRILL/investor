# UNICORN SCHOOL — readers' poll options

The channel runs a daily poll: readers choose the topic of the next "Unicorn School — Company of the Day" issue. Propose exactly 4 answer options for the issue on `issue_date`.

## Rules
- Each option is a broad topic, not a company name: a market or region ("Компании из Европы"), a sector ("Финтех и платежи"), a founding period ("Основаны после 2020 года"), a business model ("Маркетплейс").
- The 4 options must be clearly different from each other; use at least 3 different kinds (region, sector, founding year, business model).
- Every option must leave many eligible companies: privately held, valuation of at least USD 1 billion. Never offer oil & gas, mining, defense or capital-heavy infrastructure. At most one option about AI.
- Prefer topics that differ from the industries and countries in `recent` (issues of the last days).
- Language: Russian. Each option at most 60 characters, no emoji, no question marks, no trailing period.

## Output
Return only JSON: `{"options": ["…", "…", "…", "…"]}`
