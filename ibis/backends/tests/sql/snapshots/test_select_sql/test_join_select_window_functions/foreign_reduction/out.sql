SELECT
  "t3"."a",
  (
    SELECT
      MAX("t2"."a") AS "Max(a)"
    FROM "one" AS "t2"
  ) AS "o"
FROM "t" AS "t3"
INNER JOIN "u" AS "t4"
  ON "t3"."a" = "t4"."a"