Stage 11 - Cleaner recommendation cards + all-time stats page

Changes:
- New recommendations show ONLY the daily leaders block.
- Profit milestone posts show ONLY the daily leaders block.
- Weekly leaders are not embedded in recommendation/milestone cards.
- All-time general statistics are not embedded in recommendation/milestone cards.
- The All Time button still opens the channel-native All Time list.
- Page 1 of the All Time list now includes:
  * total calls published
  * cumulative PNL by ATH (sum of ATH profit percentages)
  * average ATH profit per call
  * the average formula / call count
  * real-time data note
  * note that cumulative PNL is a call-performance metric, not portfolio return
- Daily / Weekly / All Time buttons remain on recommendation and milestone posts.
- Existing PostgreSQL data is untouched. No destructive migration.
