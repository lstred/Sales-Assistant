"""Raw SQL strings used by the loaders.

Conventions:
* Always parameterize with ``:name`` markers; never f-string user values.
* Bracket non-standard identifiers (``[BSACCT]``, ``[D@MFGR]``, ``[$DESC]``).
* For *invoiced* sales we use ``INVOICE_DATE_YYYYMMDD`` and require
  ``INVOICE# > 0`` (the line actually shipped / billed).
  ``ORDER_ENTRY_DATE_YYYYMMDD`` is order-placed date and is **not** used
  for fiscal-period bucketing.
"""

# ----------------------------------------------------------------- cost centers
COST_CENTER_XREF = """
SELECT  cost_center,
        MAX(cost_center_name) AS cost_center_name,
        MAX(clyde_marketing_code) AS clyde_marketing_code
FROM (
    SELECT  LTRIM(RTRIM([CostCenter]))         AS cost_center,
            LTRIM(RTRIM([CostCenterName]))     AS cost_center_name,
            LTRIM(RTRIM([ClydeMarketingCode])) AS clyde_marketing_code
    FROM    dbo.vw_CostCenterCLydeMRKCodeXREF
    WHERE   ISNULL(LTRIM(RTRIM([CostCenter])), '') <> ''
) t
GROUP BY cost_center
ORDER BY cost_center
"""

# Master list of every cost center the warehouse actually books items into.
# The XREF view above only exposes new-system CCs that have a Clyde mapping,
# so it omits sample CCs (codes starting with '1') and any CC that's been
# created post-go-live but never mapped. We get the authoritative list from
# ITEM.[ICCTR] and left-join the XREF for the friendly name.
ALL_COST_CENTERS = """
SELECT  cost_center,
        MAX(cost_center_name) AS cost_center_name
FROM (
    SELECT  LTRIM(RTRIM(i.[ICCTR]))                AS cost_center,
            LTRIM(RTRIM(x.[CostCenterName]))       AS cost_center_name
    FROM    dbo.ITEM AS i
    LEFT JOIN dbo.vw_CostCenterCLydeMRKCodeXREF AS x
        ON  LTRIM(RTRIM(x.[CostCenter])) = LTRIM(RTRIM(i.[ICCTR]))
    WHERE   ISNULL(LTRIM(RTRIM(i.[ICCTR])), '') <> ''
      AND   i.[IINVEN] = 'Y'
) t
GROUP BY cost_center
ORDER BY cost_center
"""

# ----------------------------------------------------------------- reps roster
REPS_ROSTER = """
SELECT  LTRIM(RTRIM(s.[YSLMN#])) AS salesman_number,
        LTRIM(RTRIM(s.[YNAME]))  AS name
FROM    dbo.SALESMAN AS s
WHERE   ISNULL(LTRIM(RTRIM(s.[YSLMN#])), '') <> ''
ORDER BY s.[YNAME]
"""

# ------------------------------------------------------ rep assignments view
REP_ASSIGNMENTS = """
SELECT  LTRIM(RTRIM(b.[BSSLMN]))            AS salesman_number,
        LTRIM(RTRIM(s.[YNAME]))             AS salesman_name,
        LTRIM(RTRIM(b.[BSCODE]))            AS cost_center,
        LTRIM(RTRIM(b.[BSACCT]))            AS account_number,
        LTRIM(RTRIM(bt.[BBANK2]))           AS old_account_number,
        LTRIM(RTRIM(bt.[BNAME]))            AS account_name,
        CASE WHEN LEFT(LTRIM(ISNULL(bt.[BNAME], '')), 1) = '*'
             THEN CAST(1 AS BIT) ELSE CAST(0 AS BIT) END AS is_closed
FROM    dbo.BILLSLMN AS b
LEFT JOIN dbo.SALESMAN AS s
    ON  LTRIM(RTRIM(s.[YSLMN#])) = LTRIM(RTRIM(b.[BSSLMN]))
LEFT JOIN dbo.BILLTO AS bt
    ON  LTRIM(RTRIM(bt.[BACCT#])) = LTRIM(RTRIM(b.[BSACCT]))
WHERE   ISNULL(LTRIM(RTRIM(b.[BSACCT])), '') <> ''
  AND   ISNULL(LTRIM(RTRIM(b.[BSSLMN])), '') <> ''
  AND   ISNULL(LTRIM(RTRIM(b.[BSCODE])), '') <> ''
"""

# ------------------------------------------- invoiced sales (line-level detail)
# Used for everything that needs by-day / by-rep / by-CC bucketing.
# ``:cc_csv`` is a comma-separated list of cost-center codes; pass an empty
# string to disable the filter.
#
# Filtering rules (locked-in business logic):
#   * ``ORDER# > 0`` — discard blank/zero-order rows entirely; they are not
#     valid orders.
#   * ``INVOICE# > 0`` — only invoiced (shipped/billed) lines count toward
#     salesman credit and reported revenue. Open (unshipped) orders still
#     have an ORDER# > 0 but no invoice yet — those are loaded separately
#     via :data:`OPEN_ORDERS_LINES` for use in insights / pipeline views.
INVOICED_SALES_LINES = """
SELECT  TRY_CONVERT(int, o.[INVOICE_DATE_YYYYMMDD])         AS invoice_yyyymmdd,
        LTRIM(RTRIM(o.[ACCOUNT#I]))                          AS account_number,
        LTRIM(RTRIM(i.[ICCTR]))                              AS cost_center,
        LTRIM(RTRIM(o.[SALESPERSON_DESC]))                   AS salesperson_desc,
        TRY_CONVERT(int, o.[INVOICE#])                       AS invoice_number,
        TRY_CONVERT(int, o.[ORDER#])                         AS order_number,
        TRY_CONVERT(int, o.[LINE#I])                         AS line_number,
        TRY_CONVERT(decimal(18,2), o.[ENTENDED_PRICE_NO_FUNDS]) AS revenue,
        TRY_CONVERT(decimal(18,2), o.[LINE_GPD_WITHOUT_FUNDS])  AS gross_profit
FROM    dbo._ORDERS AS o
JOIN    dbo.ITEM    AS i ON i.[ItemNumber] = o.[ITEM_MFGR_COLOR_PAT]
WHERE   o.[N_NOT_INVENTORY] = 'Y'
  AND   i.[IINVEN] = 'Y'
  AND   TRY_CONVERT(int, o.[ACCOUNT#I]) > 1
  AND   TRY_CONVERT(int, o.[ORDER#])    > 0
  AND   TRY_CONVERT(int, o.[INVOICE#])  > 0
  AND   TRY_CONVERT(int, o.[INVOICE_DATE_YYYYMMDD]) BETWEEN :start_yyyymmdd AND :end_yyyymmdd
  AND   ( :cc_csv = ''
          OR LTRIM(RTRIM(i.[ICCTR])) IN
             (SELECT LTRIM(RTRIM(value)) FROM STRING_SPLIT(:cc_csv, ',')) )
  AND   ( :code_prefix = ''
          OR LTRIM(RTRIM(i.[ICCTR])) LIKE :code_prefix + '%' )
"""

# Open orders: real orders (ORDER# > 0) that have **not** yet been invoiced.
# Useful for pipeline / "what's about to ship" insights — never counted as
# salesman credit until the invoice posts.
OPEN_ORDERS_LINES = """
SELECT  TRY_CONVERT(int, o.[ORDER_ENTRY_DATE_YYYYMMDD])     AS order_entry_yyyymmdd,
        LTRIM(RTRIM(o.[ACCOUNT#I]))                          AS account_number,
        LTRIM(RTRIM(i.[ICCTR]))                              AS cost_center,
        LTRIM(RTRIM(o.[SALESPERSON_DESC]))                   AS salesperson_desc,
        TRY_CONVERT(int, o.[ORDER#])                         AS order_number,
        TRY_CONVERT(int, o.[LINE#I])                         AS line_number,
        TRY_CONVERT(decimal(18,2), o.[ENTENDED_PRICE_NO_FUNDS]) AS open_revenue
FROM    dbo._ORDERS AS o
JOIN    dbo.ITEM    AS i ON i.[ItemNumber] = o.[ITEM_MFGR_COLOR_PAT]
WHERE   o.[N_NOT_INVENTORY] = 'Y'
  AND   i.[IINVEN] = 'Y'
  AND   TRY_CONVERT(int, o.[ACCOUNT#I]) > 1
  AND   TRY_CONVERT(int, o.[ORDER#])    > 0
  AND   ISNULL(TRY_CONVERT(int, o.[INVOICE#]), 0) = 0
  AND   ( :cc_csv = ''
          OR LTRIM(RTRIM(i.[ICCTR])) IN
             (SELECT LTRIM(RTRIM(value)) FROM STRING_SPLIT(:cc_csv, ',')) )
  AND   ( :code_prefix = ''
          OR LTRIM(RTRIM(i.[ICCTR])) LIKE :code_prefix + '%' )
"""

# ------------------------------------- old-system summarized sales (≤ go-live)
OLD_SYSTEM_SALES = """
SELECT  LTRIM(RTRIM(h.[MarketingCode]))     AS marketing_code,
        x.[CostCenter]                       AS cost_center,
        x.[CostCenterName]                   AS cost_center_name,
        h.[FiscalYear]                       AS fiscal_year,
        LTRIM(RTRIM(h.[CustomerNumber]))     AS old_customer_number,
        LTRIM(RTRIM(bt.[BACCT#]))            AS account_number,
        LTRIM(RTRIM(bt.[BNAME]))             AS account_name,
        h.[SalesPeriod1] , h.[SalesPeriod2] , h.[SalesPeriod3] ,
        h.[SalesPeriod4] , h.[SalesPeriod5] , h.[SalesPeriod6] ,
        h.[SalesPeriod7] , h.[SalesPeriod8] , h.[SalesPeriod9] ,
        h.[SalesPeriod10], h.[SalesPeriod11], h.[SalesPeriod12],
        h.[CostsPeriod1] , h.[CostsPeriod2] , h.[CostsPeriod3] ,
        h.[CostsPeriod4] , h.[CostsPeriod5] , h.[CostsPeriod6] ,
        h.[CostsPeriod7] , h.[CostsPeriod8] , h.[CostsPeriod9] ,
        h.[CostsPeriod10], h.[CostsPeriod11], h.[CostsPeriod12],
        h.[TotalSales], h.[TotalCost], h.[Profit]
FROM    dbo.ClydeMarketingHistory AS h
JOIN    dbo.vw_CostCenterCLydeMRKCodeXREF AS x
    ON  LTRIM(RTRIM(x.[ClydeMarketingCode])) = LTRIM(RTRIM(h.[MarketingCode]))
LEFT JOIN dbo.BILLTO AS bt
    ON  LTRIM(RTRIM(bt.[BBANK2])) = LTRIM(RTRIM(h.[CustomerNumber]))
WHERE   h.[FiscalYear] BETWEEN :fy_start AND :fy_end
"""

# ----------------------------------------------------- displays (CLASSES + BCACCT)
DISPLAY_TYPES = """
SELECT  LTRIM(RTRIM([CLCODE])) AS display_code,
        LTRIM(RTRIM([CLDESC])) AS display_desc
FROM    dbo.CLASSES
WHERE   LTRIM(RTRIM([CLCAT])) = 'DT'
ORDER BY [CLCODE]
"""

DISPLAY_PLACEMENTS = """
SELECT  LTRIM(RTRIM(b.[BCACCT]))   AS account_number,
        LTRIM(RTRIM(b.[BCCODE]))   AS display_code,
        LTRIM(RTRIM(c.[CLDESC]))   AS display_desc,
        b.[DateFormatted]          AS placed_on
FROM    dbo.BCACCT AS b
LEFT JOIN dbo.CLASSES AS c
    ON  LTRIM(RTRIM(c.[CLCODE])) = LTRIM(RTRIM(b.[BCCODE]))
   AND  LTRIM(RTRIM(c.[CLCAT]))  = 'DT'
WHERE   LTRIM(RTRIM(b.[BCCAT])) = 'DT'
  AND   ISNULL(LTRIM(RTRIM(b.[BCACCT])), '') <> ''
"""
