SELECT
  *
FROM "t" AS "t0"
WHERE
  EXISTS(
    SELECT
      1
    FROM "one" AS "t1"
  )