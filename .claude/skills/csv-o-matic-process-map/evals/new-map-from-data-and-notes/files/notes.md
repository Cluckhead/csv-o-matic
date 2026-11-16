# pricing job - my notes

- vendor drops a file into 01_vendor every evening. if they send a correction it comes as another
  file for the same day with the next number on the end. we always use the highest number.
- validate step: only CLOSE prices are kept (they also send BID/ASK/MID, we ignore those).
  anything with a price of zero or below is thrown away. GBX is converted to GBP (divide by 100),
  currency column changes to GBP.
- publish step: only securities that are in the golden security master for that date go out
  (matched on sedol), everything else is left behind. published file is one per date, it gets
  overwritten if we rerun.
- valuation team picks up 03_published.
