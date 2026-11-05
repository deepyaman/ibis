SELECT
  *
FROM "t" AS "t0"
WHERE
  "t0"."a" > (
    SELECT
      *
    FROM "one" AS "t2"
  )