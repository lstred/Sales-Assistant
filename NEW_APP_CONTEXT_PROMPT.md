# New App Context Prompt — SQL Server, Tables, Fields & Tab Definitions

Use this prompt as the starting-context block when building the new application.
It documents the SQL Server connection pattern, every table and field used by the existing
Inventory Dashboard, and the full field-level definitions for the **Overview** and
**Stock Turn** tabs.

---

## 1. SQL Server Connection Setup

### Stack
- **ORM / Driver**: SQLAlchemy with `mssql+pyodbc` dialect
- **ODBC Driver**: `ODBC Driver 18 for SQL Server` (must be installed on host)
- **Authentication**: Windows Trusted Connection (no username/password)
- **Engine options**: `fast_executemany=True`, `pool_pre_ping=True`

### Default Connection String
```
Driver={ODBC Driver 18 for SQL Server};
Server=NRFVMSSQL04;
Database=NRF_REPORTS;
Trusted_Connection=Yes;
Encrypt=no;
```

### Connection String Resolution Order (highest → lowest priority)
1. Environment variable `SQLSERVER_ODBC`
2. `%APPDATA%\PurchaseOrderBot\config.json` → key `"SQLSERVER_ODBC"`
3. `config_local.py` alongside project root → attribute `SQLSERVER_ODBC`

### SQLAlchemy URL Construction
```python
from urllib.parse import quote_plus
odbc_url = f"mssql+pyodbc:///?odbc_connect={quote_plus(connection_string)}"
engine = create_engine(odbc_url, fast_executemany=True, pool_pre_ping=True)
```

### Helper Pattern
```python
# Execute a query and return a pandas DataFrame
def read_dataframe(connection_string, sql, params=None):
    with engine.connect() as conn:
        return pd.read_sql_query(text(sql), conn, params=params)
```

### Required Python Packages
```
pip install sqlalchemy pyodbc pandas
```
`pyodbc` is the Python ODBC bridge. `ODBC Driver 18 for SQL Server` is an **OS-level driver** — it is NOT a pip package. It must be installed separately on the Windows machine.

Download: https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server

### Step-by-Step Connection Setup

1. **Install ODBC Driver 18 for SQL Server** — Download and run the MSI from Microsoft. Confirm installation by opening ODBC Data Sources (64-bit) and checking the Drivers tab for "ODBC Driver 18 for SQL Server".

2. **Install Python packages** — `pip install sqlalchemy pyodbc pandas`

3. **Create `config_local.py`** in the project root (this file is gitignored):
   ```python
   SQLSERVER_ODBC = (
       "Driver={ODBC Driver 18 for SQL Server};"
       "Server=NRFVMSSQL04;"
       "Database=NRF_REPORTS;"
       "Trusted_Connection=Yes;"
       "Encrypt=no;"
   )
   ```

4. **Verify the connection** with a quick test:
   ```python
   from sqlalchemy import create_engine, text
   from urllib.parse import quote_plus
   from config_local import SQLSERVER_ODBC

   engine = create_engine(
       f"mssql+pyodbc:///?odbc_connect={quote_plus(SQLSERVER_ODBC)}",
       fast_executemany=True,
       pool_pre_ping=True,
   )
   with engine.connect() as conn:
       row = conn.execute(text("SELECT DB_NAME() AS db, SYSTEM_USER AS usr")).fetchone()
       print(row)  # ('NRF_REPORTS', 'DOMAIN\\your_username')
   ```

> **Network requirement**: The machine must be on the NRF corporate network or VPN. The server uses Windows authentication only — there are no SQL logins or passwords.

### Parameterized Query Pattern (always use this — never f-strings for user values)
```python
sql = "SELECT * FROM dbo._ORDERS WHERE [ITEM_MFGR_COLOR_PAT] = :sku AND [IINVEN] = 'Y'"
with engine.connect() as conn:
    df = pd.read_sql_query(text(sql), conn, params={"sku": "SOMESKU123"})
```
Parameters use `:name` syntax with `sqlalchemy.text()`. Never inject values via string formatting.

---

## 2. Field Nicknames — Quick Reference

These are the shorthand names / aliases used throughout code, conversations, and the UI. When you see these terms in a prompt or comment, this is what they map to in the database.

| Nickname / Alias | DB Column | Table | Notes |
|---|---|---|---|
| `sku` | `ITEM_MFGR_COLOR_PAT` | `_ORDERS` | Also `ItemNumber` in `ITEM`, `ROLLS`, `ITEMSTK` |
| `order_entry_date` | `ORDER_ENTRY_DATE_YYYYMMDD` | `_ORDERS` | Parsed from YYYYMMDD integer |
| `actual_ship_date` | `INVOICE_SHIP_DATE` or `ORDER_SHIP_DATE` | `_ORDERS` | Use INVOICE_SHIP_DATE when INVOICE# > 0 |
| `extended_price` / `revenue` | `ENTENDED_PRICE_NO_FUNDS` | `_ORDERS` | Note the permanent typo: ENTENDED not EXTENDED |
| `gross_profit` / `GP` | `LINE_GPD_WITHOUT_FUNDS` | `_ORDERS` | GP before marketing fund credits |
| `GP with funds` / `GPP` | `LINE_GPP_WITH_FUNDS` | `_ORDERS` | GP after adding fund credits |
| `backorder_flag` | `DETAIL_LINE_STATUS` | `_ORDERS` | True when value is `'B'` or `'R'` |
| `eta_date` | `PO_ETA_DATE` | `_ORDERS` | Expected delivery date for POs |
| `customer_name` | `BANK_NAME2` | `_ORDERS` | The customer name field |
| `salesperson` | `SALESPERSON_DESC` | `_ORDERS` | Rep name as text |
| `quantity_sy` | `QUANTITY_ORDERED` + UOM conversion | `_ORDERS` | Derived — all quantities normalized to SY |
| `order_line_id` | `ORDER# + "-" + LINE#I` | `_ORDERS` | Composite key for deduplication |
| `cost_center` / `CC` | `ICCTR` | `ITEM` | Division code, e.g. `'010'` |
| `price_class` / `PC` | `IPRCCD` | `ITEM` | Product pricing group |
| `price_class_desc` | `$DESC` | `PRICE` | Human name for the price class |
| `sku_description` | `INAME` | `ITEM` | Item description / name |
| `manufacturer` / `mfgr` | `IMFGR` | `ITEM` | Manufacturer code |
| `product_line` | `IPRODL` | `ITEM` | Product line code |
| `item_width_inches` | `IWIDTH` | `ITEM` | Roll width; 0 = unknown |
| `lead_time_days` | `IDELIV` | `ITEM` | Item-level; falls back to `PRODLINE.LDELIV` then 33 |
| `inventory_flag` | `IINVEN` | `ITEM` | `'Y'` = active inventory item |
| `iixref` / `base_sku` | `IIXREF` | `ITEM` | If set: this SKU is an alias; value is the base SKU |
| `discontinued` | `IDISCD` | `ITEM` | Non-zero = discontinued |
| `dropped_item` / `DI` | `IPOL1/2/3 = 'DI'` | `ITEM` | Policy flag value meaning "Dropped Item" |
| `supplier_number` | `ISUPP#` | `ITEM` | Default/usual supplier |
| `available_quantity` | `Available` | `ROLLS` | Qty in warehouse; convert to SY using RUM + IWIDTH |
| `inventory_sy` | `Available` (converted) | `ROLLS` | Derived — warehouse stock in SY |
| `receive_date` | `RLRCTD` | `ROLLS` | When the roll was received |
| `location` | `RLOC1` | `ROLLS` | `'REM'` = remnant → exclude |
| `status_code` | `RCODE@` | `ROLLS` | `'#'` or contains `'I'` → exclude |
| `jstock` | `JSTOCK` | `ITEMSTK` | System-set stock target quantity |
| `total_cost` | `TotalCost` | `_INVENTORY` | Total inventory cost for the SKU |
| `product_line_lead_time` | `LDELIV` | `PRODLINE` | Fallback lead time if `ITEM.IDELIV` is blank |
| `credit_type_desc` | `CLDESC` | `CLASSES` | Description of a return/credit type code |
| `account_number` / `acct` | `ACCOUNT#I` | `_ORDERS` | `1` = PO (warehouse); `>1` = customer |
| `salesman_number` | `BSSLMN` | `BILLSLMN` | Sales rep ID in assignment table |
| `BSCODE` | `BSCODE` | `BILLSLMN` | Cost center of the assignment (same values as `ICCTR`) |
| `gl_number` | `M@GL#` | `OPENPO_M` | `9140` = restocking fee |
| `fee_amount` | `M@MISP` | `OPENPO_M` | Dollar amount of a PO fee/charge |
| `pending_qty` | `D@QTYO - D@QTYP` | `OPENPO_D` | Net quantity still expected on a PO |

---

## 2b. Database Tables and Fields

All tables are in the `dbo` schema of `NRF_REPORTS`.

---

### `dbo._ORDERS` — Sales Orders & Purchase Orders (line items)

This is the central fact table. Each row is one order line.

| DB Column | Alias used in app | Description |
|---|---|---|
| `ITEM_MFGR_COLOR_PAT` | `sku` | SKU identifier (FK → `ITEM.ItemNumber`) |
| `QUANTITY_ORDERED` | `quantity_ordered` | Raw ordered quantity in native unit of measure |
| `UNIT_OF_MEASURE` | `unit_of_measure` | UOM code (SY, SF, LY, LF, IN, etc.) |
| `ORDER_SHIP_DATE` | `order_ship_date` | Requested ship date (datetime) |
| `INVOICE_SHIP_DATE` | `invoice_ship_date` | Actual ship date from invoice (datetime) |
| `ORDER#` | `order_number` | Order number |
| `LINE#I` | `line_number` | Line number within order |
| `ACCOUNT#I` | `account_number` | Account number. Value `1` = internal purchase order; values > 1 = customer sales orders |
| `BANK_NAME2` | `bank_name2` / `customer_name` | Customer name |
| `CUSTOMER_PO#` | `customer_po` | Customer purchase order number |
| `ORDER_TYPE` | `order_type` | Order type code |
| `RESTOCKING_CHARGE_P` | `restocking_charge_p` | Restocking fee percentage |
| `DISCOUNT_HANDLING_CHARGED` | `discount_handling_charged` | Discount/handling amount charged |
| `ENTENDED_PRICE_NO_FUNDS` | `extended_price_no_funds` / `extended_price_usd` | Extended price (revenue) excluding fund discounts |
| `ITEM_WIDTH_INCHES_IF_R` | `item_width_inches` | Item width in inches (populated only for roll goods) |
| `N_NOT_INVENTORY` | `not_inventory_flag` | `'Y'` = inventory item (filter always applied) |
| `ORDER_ENTRY_DATE_YYYYMMDD` | `order_entry_date_raw` | Order entry date as YYYYMMDD integer/string |
| `DETAIL_LINE_STATUS` | `detail_line_status` | Line status code. `'B'` = backordered, `'R'` = reserved/backorder, blank/other = shipped/open |
| `PO_ETA_DATE` | `po_eta_date` / `eta_date` | Expected arrival date for purchase orders |
| `SUPPLIER#` | `supplier_number` | Supplier code on the order line |
| `USUAL_SUPPLIER` | `usual_supplier` | Usual supplier code linked to the item |
| `INVOICE#` | `invoice_number` | Invoice number (numeric > 0 means shipped/invoiced) |
| `SALESPERSON_DESC` | `salesperson_desc` / `salesperson` | Salesperson name |
| `COST_CENTER_DESC` | `cost_center_desc` | Cost center description text |
| `CREDIT_TYPE_CODE` | `credit_type_code` | Credit type code (FK → `CLASSES.CLCODE` where `CLCAT='CC'`) |
| `REASON_CODE` | `reason_code` | Reason code for returns/credits |
| `ORDER_REASON_CODE_DESC` | `order_reason_code_desc` | Reason code description |
| `ORDER_DATE` | `order_date` | Order date in standard SQL date format |
| `ORDER_DATE_MMDDYY` | `order_date_mmddyy` | Order date in MM/DD/YY format |
| `LINE_GPP_WITH_FUNDS` | `line_gpp_with_funds` | Gross profit including fund discounts |
| `LINE_GPD_WITHOUT_FUNDS` | `line_gross_profit` / `gross_profit_usd` | Gross profit excluding fund discounts |
| `ORDER_REFERENCE#` | `order_reference` | Order reference number |
| `ITEM_DESC_1` | `item_desc_1` / `item_description` | Item description line 1 |
| `PRICE_PER_UM` | `price_per_um` | Unit price |
| `COST_PER_UM` | `cost_per_um` / `line_cost_per_unit` | Unit cost |
| `ITEM_CLASS_1_DESC` | `item_class_1_desc` / `product_category` | Item classification level 1 |
| `ITEM_CLASS_2_DESC` | `item_class_2_desc` | Item classification level 2 |
| `ITEM_CLASS_3_DESC` | `item_class_3_desc` | Item classification level 3 |
| `INVOICE_DATE_YYYYMMDD` | `invoice_date_raw` | Invoice date as YYYYMMDD |

**Key filters always applied:**
- `N_NOT_INVENTORY = 'Y'` (inventory items only)
- `ITEM.IINVEN = 'Y'` (active inventory flag on item master)
- Sales velocity uses only `ACCOUNT#I > 1` (exclude internal PO lines)
- Open Orders filter uses only supplier `'001'`

**Derived columns:**
- `order_entry_date`: parsed from `ORDER_ENTRY_DATE_YYYYMMDD` using format `%Y%m%d`
- `actual_ship_date`: = `INVOICE_SHIP_DATE` when `INVOICE# > 0`, else `ORDER_SHIP_DATE`
- `backorder_flag`: `True` when `DETAIL_LINE_STATUS` is exactly `'B'` or `'R'`
- `order_line_id`: `order_number + "-" + line_number` (composite key for deduplication)
- `quantity_sy`: `QUANTITY_ORDERED` converted to square yards (see Unit Conversion section)

---

### `dbo.ITEM` — Item Master

| DB Column | Alias | Description |
|---|---|---|
| `ItemNumber` | `sku` | Primary key — SKU identifier |
| `IPRCCD` | `price_class` | Price class code (FK → `PRICE.$PRCCD`) |
| `ICCTR` | `cost_center` | Cost center code (e.g. `'010'`, `'012'`) |
| `IPRODL` | `product_line` | Product line code (FK → `PRODLINE.LPROD#`) |
| `IMFGR` | `manufacturer` | Manufacturer code (FK → `PRODLINE.LMFGR#`) |
| `INAME` | `sku_description` | Item description / name |
| `IPATT` | `item_pattern` | Pattern code |
| `ISUPP#` | `supplier_number` | Default supplier for this item |
| `IDELIV` | `item_lead_time_days` | Item-level lead time in days |
| `IWIDTH` | `item_width_inches` | Item width in inches (roll goods) |
| `IINVEN` | `inventory_flag` | `'Y'` = active inventory item |
| `IIXREF` | `iixref` | Cross-reference SKU: if set, this item is an alias; the base SKU is the IIXREF value. Used to consolidate alias SKUs into a single base SKU for inventory/sales aggregation |
| `IDISCD` | `discontinued_date_raw` / `discontinued_flag` | Discontinuation date as numeric; non-zero = discontinued |
| `IPOL1`, `IPOL2`, `IPOL3` | — | Policy flags; value `'DI'` = "Dropped Item" |

**Active item filter:** `IINVEN = 'Y'` AND `IDISCD` is null/blank/`'0'`

---

### `dbo.ROLLS` — Physical Inventory Rolls

Each row is a physical roll in the warehouse.

| DB Column | Alias | Description |
|---|---|---|
| `ItemNumber` | `sku` | SKU identifier |
| `Available` | `available_quantity` | Available quantity in native UOM |
| `RUM` | `unit_of_measure` | Unit of measure for this roll |
| `RROLL#` | `roll_number` | Roll number |
| `RLOC1` | `location` | Warehouse location code. `'REM'` = remnant (excluded) |
| `RCODE@` | `status_code` | Status code. `'#'` = inactive/reserved (excluded). Rows containing `'I'` in status are also excluded |
| `RLRCTD` | `receive_date` | Date roll was received (used for inventory age calculation) |

**Filters applied:**
- `Available > 0`
- `location != 'REM'`
- `status_code != '#'`
- Status does not contain `'I'`
- Only SKUs where `ITEM.IINVEN = 'Y'`

**Derived:**
- `inventory_sy`: `available_quantity` converted to SY using width from ITEM.IWIDTH
- `age_days`: `today - receive_date` in days
- `inventory_age_days` (per SKU): weighted average age = Σ(inventory_sy × age_days) / Σ(inventory_sy)

---

### `dbo.OPENIV` — Open Receipts (Goods Received)

| DB Column | Alias | Description |
|---|---|---|
| `NREFTY` | `ref_type` | Reference type. `'R'` = receipt |
| `NDATE` | `receipt_date` | Receipt date |
| `NPO#` | `purchase_order_number` | PO number (links to `_ORDERS.ORDER#`) |
| `NRECEI` | `quantity_received` | Quantity received |
| `NMFGR` | `mfgr_part` | Manufacturer part of SKU |
| `NCOLOR` | `color_part` | Color part of SKU |
| `NPAT` | `pattern_part` | Pattern part of SKU |

**Filter:** `NREFTY = 'R'`

---

### `dbo.OPENPO_D` — Pending Purchase Order Detail

| DB Column | Alias | Description |
|---|---|---|
| `D@MFGR` | `mfgr` | Manufacturer component of SKU |
| `D@COLO` | `colo` | Color component of SKU |
| `D@PATT` | `patt` | Pattern component of SKU |
| `D@QTYO` | `qty_ordered` | Quantity ordered on this PO line |
| `D@QTYP` | `qty_posted` | Quantity posted/received so far |
| `D@ACCT` | `acct` | Account number. `1` = warehouse PO |
| `D@DEL8` | `del8` | Delivery flag. `'#'` = deleted (excluded) |
| `D@SUPP` | `supp` | Supplier code. `'001'` = excluded from pending |
| `D@REF#` | — | PO reference number (must be valid integer > 0) |

**Derived:** `sku` = `MFGR + COLO + PATT` (concatenated); `po_pending_qty` = `qty_ordered - qty_posted` (in SY)

**Partials filter:** `ACCT=1`, `del8 != '#'`, `qty_posted > 0`
**Pending filter:** `ACCT=1`, `del8 != '#'`, `supp != '001'`, `ref# > 0`

---

### `dbo.OPENPO_M` — PO Message / Fee Lines

| DB Column | Alias | Description |
|---|---|---|
| `M@REF#` | `order_number` | PO reference number |
| `M@LINE` | `line_number` | Line number |
| `M@GL#` | `gl_number` | GL account number. `9140` = restocking fee |
| `M@MISP` | `fee_amount` | Fee amount |
| `M@MSG` | `message_text` | Message text (used for return reason identification) |

---

### `dbo.PRODLINE` — Product Lines

| DB Column | Alias | Description |
|---|---|---|
| `LPROD#` | `product_line` | Product line code |
| `LMFGR#` | `manufacturer` | Manufacturer code |
| `LNAME` | `product_line_desc` | Product line description |
| `LDELIV` | `product_line_lead_time_days` | Default lead time in days for this product line |

**Relationship:** `ITEM.IPRODL + ITEM.IMFGR` → `PRODLINE.LPROD# + PRODLINE.LMFGR#`

---

### `dbo.PRICE` — Price Classes

| DB Column | Alias | Description |
|---|---|---|
| `$PRCCD` | `price_class` | Price class code |
| `$LIST#` | — | List type. Always filter: `$LIST# = 'LP'` |
| `$DESC` | `price_class_desc` | Price class description / name |

**Relationship:** `ITEM.IPRCCD` → `PRICE.$PRCCD` (where `$LIST# = 'LP'`)

---

### `dbo.CLASSES` — Code Lookup Table

| DB Column | Alias | Description |
|---|---|---|
| `CLCAT` | — | Category code. `'CC'` = credit type |
| `CLCODE` | `credit_type_code` | The code value |
| `CLDESC` | `credit_type_desc` | Human-readable description of the code |

**Relationship:** `_ORDERS.CREDIT_TYPE_CODE` → `CLASSES.CLCODE` (where `CLCAT = 'CC'`)

---

### `dbo.ITEMSTK` — Item Stock Targets

| DB Column | Alias | Description |
|---|---|---|
| `ItemNumber` | `sku` | SKU identifier |
| `JSTOCK` | `jstock` | Target stock quantity (system-set stock turn target) |

---

### `dbo._INVENTORY` — Inventory Cost View

| DB Column | Alias | Description |
|---|---|---|
| `Item` | `sku` | SKU identifier |
| `TotalCost` | `total_cost` | Total cost of current inventory for this SKU |

**Filter:** `ITEM.IINVEN = 'Y'` AND `TotalCost > 0`

---

### `dbo.BILLSLMN` — Salesperson Account Assignments

Maps customer accounts to the sales rep and cost center responsible for them. The source of truth for sales rep peer grouping and coverage analysis.

| DB Column | Alias | Description |
|---|---|---|
| `BSACCT` | `account_number` | Customer account number. FK → `_ORDERS.ACCOUNT#I` |
| `BSSLMN` | `salesman_number` | Sales rep identifier — matches `_ORDERS.SALESPERSON_DESC` indirectly |
| `BSCODE` | `cost_center` | Cost center this assignment belongs to. Same concept as `ITEM.ICCTR` but in the sales context |

**Standard filter:** All three columns must be non-null and non-blank.

**Use:** Determine which rep is responsible for which accounts, and calculate BSCODE overlap between reps for peer grouping (≥60% Jaccard similarity = peers).

**Nickname note:** `BSCODE` is just a cost center code — the same codes you see in `ITEM.ICCTR` (e.g., `010`, `012`). When used in the context of this table it's called `BSCODE` but it refers to the same business divisions.

---

### `dbo.BILL_CD` — Account Group / Category Codes

Used exclusively to identify accounts that belong to the CCA (Customer Category Account) program.

| DB Column | Alias | Description |
|---|---|---|
| `BCACCT` | `account_number` | Customer account. FK → `_ORDERS.ACCOUNT#I` |
| `BCCODE` | `group_code` | Group membership code. CCA members have codes: `'ACA'`, `'ACP'`, `'AC1'` |
| `BCCAT` | `category_code` | Category filter. Always filter to `'MP'` for CCA purposes |

**CCA account filter:**
```sql
WHERE LTRIM(RTRIM(ISNULL(BCCAT, ''))) = 'MP'
  AND LTRIM(RTRIM(ISNULL(BCCODE, ''))) IN ('ACA', 'ACP', 'AC1')
  AND LTRIM(RTRIM(ISNULL(BCACCT, ''))) <> ''
```

---

## 3. Table Relationships Summary

```
_ORDERS.ITEM_MFGR_COLOR_PAT  ─────→  ITEM.ItemNumber
_ORDERS.CREDIT_TYPE_CODE      ─────→  CLASSES.CLCODE  (where CLCAT='CC')
_ORDERS.ACCOUNT#I             ─────→  BILLSLMN.BSACCT (account ↔ rep lookup)
_ORDERS.ACCOUNT#I             ─────→  BILL_CD.BCACCT  (CCA membership check)
ITEM.IPRCCD                   ─────→  PRICE.$PRCCD    (where $LIST#='LP')
ITEM.IPRODL + ITEM.IMFGR      ─────→  PRODLINE.LPROD# + PRODLINE.LMFGR#
ITEM.IIXREF                   ─────→  ITEM.ItemNumber (self-ref alias → base SKU)
ROLLS.ItemNumber               ─────→  ITEM.ItemNumber
ITEMSTK.ItemNumber             ─────→  ITEM.ItemNumber
_INVENTORY.Item               ─────→  ITEM.ItemNumber
OPENIV.NPO#                   ─────→  _ORDERS.ORDER#  (receipt match)
OPENPO_D: D@MFGR+D@COLO+D@PATT ───→  ITEM.ItemNumber (SKU = mfgr+color+pattern)
```

### Common JOIN Patterns (copy-paste ready)

**Orders + Item Master** (the most common join):
```sql
SELECT o.[ITEM_MFGR_COLOR_PAT] AS sku,
       o.[QUANTITY_ORDERED],
       o.[UNIT_OF_MEASURE],
       i.[IWIDTH],
       i.[ICCTR]                AS cost_center,
       i.[IPRCCD]               AS price_class
FROM dbo._ORDERS AS o
INNER JOIN dbo.ITEM AS i
    ON i.[ItemNumber] = o.[ITEM_MFGR_COLOR_PAT]
WHERE o.[N_NOT_INVENTORY] = 'Y'
  AND i.[IINVEN] = 'Y'
  AND TRY_CONVERT(int, o.[ACCOUNT#I]) > 1   -- customer sales only (exclude POs)
```

**Item + Price Class description:**
```sql
LEFT JOIN dbo.PRICE AS p
    ON p.[$PRCCD] = i.[IPRCCD]
   AND p.[$LIST#] = 'LP'
-- p.[$DESC] = price_class_desc
```

**Item + Product Line (for lead time):**
```sql
LEFT JOIN dbo.PRODLINE AS pr
    ON pr.[LPROD#] = i.[IPRODL]
   AND pr.[LMFGR#] = i.[IMFGR]
-- pr.[LDELIV] = product_line_lead_time_days (fallback when ITEM.IDELIV is blank)
```

**Orders + Credit type lookup (Returns tab):**
```sql
LEFT JOIN dbo.CLASSES AS cl
    ON cl.[CLCODE] = o.[CREDIT_TYPE_CODE]
   AND cl.[CLCAT]  = 'CC'
-- cl.[CLDESC] = credit_type_desc
```

**Rolls + Item (current warehouse stock):**
```sql
SELECT r.[ItemNumber] AS sku,
       r.[Available],
       r.[RUM]        AS unit_of_measure,
       r.[RLRCTD]     AS receive_date,
       i.[IWIDTH]     AS item_width_inches
FROM dbo.ROLLS AS r
INNER JOIN dbo.ITEM AS i
    ON i.[ItemNumber] = r.[ItemNumber]
WHERE r.[Available] > 0
  AND ISNULL(r.[RLOC1], '') <> 'REM'    -- exclude remnants
  AND ISNULL(r.[RCODE@], '#') <> '#'    -- exclude inactive
  AND i.[IINVEN] = 'Y'
```

**Lead time resolution (item → prodline → default 33 days):**
```sql
-- In Python after loading both tables:
lead_days = item_row["IDELIV"] or prodline_row["LDELIV"] or 33
```

**OPENPO_D (pending PO quantities on order not yet received):**
```sql
SELECT LTRIM(RTRIM([D@MFGR])) + LTRIM(RTRIM([D@COLO])) + LTRIM(RTRIM([D@PATT])) AS sku,
       [D@QTYO] - [D@QTYP]  AS pending_qty,
       [D@SUPP]              AS supplier
FROM dbo.OPENPO_D
WHERE [D@ACCT]  = 1             -- warehouse PO
  AND [D@DEL8] <> '#'           -- not deleted
  AND [D@SUPP] <> '001'         -- exclude internal supplier
  AND TRY_CONVERT(int, NULLIF(LTRIM(RTRIM([D@REF#])), '')) > 0
```

**Account rep assignments (BILLSLMN):**
```sql
SELECT [BSACCT] AS account_number,
       [BSSLMN] AS salesman_number,
       [BSCODE] AS cost_center
FROM dbo.BILLSLMN
WHERE LTRIM(RTRIM(ISNULL([BSACCT], ''))) <> ''
  AND LTRIM(RTRIM(ISNULL([BSSLMN], ''))) <> ''
  AND LTRIM(RTRIM(ISNULL([BSCODE], ''))) <> ''
```

---

## 3b. Field Quirks & Gotchas

These are the most common stumbling blocks when working with this database.

| Field / Table | Quirk | Correct Approach |
|---|---|---|
| `_ORDERS.ENTENDED_PRICE_NO_FUNDS` | **Permanent typo** — the column is `ENTENDED`, not `EXTENDED`. The database has always had this misspelling. | Always use `ENTENDED_PRICE_NO_FUNDS` exactly as spelled |
| `_ORDERS.N_NOT_INVENTORY` | **Backwards name** — `'Y'` means this IS an inventory item, not "not inventory". Confusing but correct. | Always filter `N_NOT_INVENTORY = 'Y'` to get inventory items |
| `_ORDERS.ORDER_ENTRY_DATE_YYYYMMDD` | Stored as a **numeric YYYYMMDD integer**, not a SQL date type. Cannot be compared directly to dates. | Parse in Python: `pd.to_datetime(df["ORDER_ENTRY_DATE_YYYYMMDD"].astype(str), format="%Y%m%d", errors="coerce")` |
| `_ORDERS.INVOICE_DATE_YYYYMMDD` | Same YYYYMMDD integer storage issue as above. | Same fix as above |
| `_ORDERS.ACCOUNT#I` | **`1` = warehouse purchase order**, not a customer. Including these in sales metrics silently inflates numbers. | Always filter `ACCOUNT#I > 1` for customer sales; use `= 1` for POs only |
| `_ORDERS.INVOICE#` | Not stored as a numeric type — must use `TRY_CONVERT(int, ...)` to check if the line has been invoiced. | `TRY_CONVERT(int, NULLIF(LTRIM(RTRIM([INVOICE#])), '')) > 0` means shipped |
| `ITEM.IIXREF` | If this column is non-empty, the item is an **alias SKU**. The actual base SKU is the `IIXREF` value. Failing to resolve this causes double-counting of inventory and sales. | Before any groupby, replace `sku` with `IIXREF` value if `IIXREF` is populated |
| `ITEM.IWIDTH` | Width = `0` means **unknown**, not actually zero inches. Zero width would cause division-by-zero in SY conversion. | Treat `IWIDTH = 0` as `NaN` / missing |
| `ITEM.IDISCD` | Discontinuation date stored as a numeric/text field. Non-zero means discontinued, but the check requires `TRY_CONVERT`. | Filter: `TRY_CONVERT(int, NULLIF(LTRIM(RTRIM([IDISCD])), '')) = 0 OR [IDISCD] IS NULL` |
| `ROLLS.RCODE@` | `'#'` means the roll is **inactive** — exclude it even if `Available > 0`. Also exclude any status code containing the letter `'I'`. | `WHERE ISNULL([RCODE@], '#') <> '#'` plus Python-side check for `'I'` in status |
| `ROLLS.RLOC1` | `'REM'` = remnant. Remnant rolls should be excluded from all inventory metrics. | `WHERE ISNULL([RLOC1], '') <> 'REM'` |
| `OPENPO_D` column names | Column names use `@` symbol: `D@MFGR`, `D@COLO`, `D@PATT`, `D@QTYO`, etc. These are valid SQL column names but must be quoted with brackets `[]`. | Always bracket them: `[D@MFGR]`, `[D@QTYO]` |
| `OPENPO_D` SKU assembly | The SKU is NOT stored as a single field — it must be assembled: `D@MFGR + D@COLO + D@PATT`. Strip whitespace from each part first. | `LTRIM(RTRIM([D@MFGR])) + LTRIM(RTRIM([D@COLO])) + LTRIM(RTRIM([D@PATT]))` |
| `OPENIV` SKU assembly | Same pattern: `NMFGR + NCOLOR + NPAT` (strip each part). | `LTRIM(RTRIM([NMFGR])) + LTRIM(RTRIM([NCOLOR])) + LTRIM(RTRIM([NPAT]))` |
| `PRICE.$LIST#` | The PRICE table has one row per price class **per list type**. If you forget to filter `$LIST# = 'LP'`, you'll get duplicate rows and inflated joins. | Always add `AND [$LIST#] = 'LP'` when joining PRICE |
| `PRICE.$PRCCD` / `PRICE.$DESC` | Column names start with `$` — must be bracketed in SQL. | `[$PRCCD]`, `[$DESC]`, `[$LIST#]` |
| Cost centers starting with `'1'` | These are internal/system cost centers, not real business divisions. They must be excluded from all reporting. | Filter: `LEFT(ISNULL([ICCTR], ''), 1) <> '1'` |
| Numeric columns stored as text | Many "numeric" columns in `_ORDERS` are actually `VARCHAR` with possible spaces, nulls, or non-numeric values. Direct `CAST` will fail. | Always use `TRY_CONVERT(int/decimal, NULLIF(LTRIM(RTRIM(col)), ''))` |
| Supplier `'001'` | This is the internal/house supplier. It is special-cased throughout: Open Orders tab shows **only** 001 orders; pending OPENPO_D **excludes** 001. Know which context you're in. | Confirm before filtering whether you want to include or exclude 001 |

---

## 4. Unit Conversion — All Quantities Standardized to Square Yards (SY)

All quantity fields (`quantity_ordered`, `available_quantity`, etc.) are converted to **SY** before any metric calculation.

| Input UOM | Condition | Conversion |
|---|---|---|
| SY, SQY, SQYD, SQYDS | Any | qty × 1 (already SY) |
| SF, SQF, FT2, SQFT | Cost center in `010`, `011`, `012`, `013` | qty ÷ 9 |
| SF, SQF, FT2, SQFT | Other cost centers | qty × 1 (no conversion) |
| LY, YD, YDS, YARD | Width available | (qty × width_inches) ÷ 36 |
| LY, YD, YDS, YARD | No width | qty × 1 (raw) |
| LF, FT, FEET, FOOT | Width available | (qty × width_inches) ÷ 108 |
| IN, INCH, INCHES | Width available | (qty × width_inches) ÷ 1296 |
| Other | — | qty × 1 |

Width source priority: `_ORDERS.ITEM_WIDTH_INCHES_IF_R` → `ITEM.IWIDTH` (via base_sku map)

---

## 5. SKU Alias / Base SKU Logic

- `ITEM.IIXREF` stores a cross-reference SKU for alias items.
- If `IIXREF` is populated, that item is an **alias**; `IIXREF` value is the **base SKU**.
- All sales, inventory, and PO data is remapped to the base SKU before aggregation.
- The ITEM table is queried twice: once CC-scoped (for metrics), once globally (for attribute enrichment).

---

## 6. Core Computed Metrics (per-SKU, used across both tabs)

| Metric | Formula | Source columns |
|---|---|---|
| `avg_daily_sales_sy` | `total_quantity_sy / days_in_window` | `_ORDERS.QUANTITY_ORDERED` → `quantity_sy` |
| `orders_count` | Distinct order lines (order_line_id) | `_ORDERS` |
| `backorder_count` | Distinct order lines where status is `'B'` or `'R'` | `_ORDERS.DETAIL_LINE_STATUS` |
| `backorder_qty_sy` | Sum of `quantity_sy` where status = `'B'` only | `_ORDERS.DETAIL_LINE_STATUS` |
| `inventory_sy` | Sum of available roll quantities in SY | `ROLLS.Available` |
| `on_order_sy` | Sum of PO quantities in SY (ACCOUNT#I=1) | `_ORDERS.QUANTITY_ORDERED` |
| `po_pending_qty` | Σ(qty_ordered - qty_posted) from OPENPO_D | `OPENPO_D.D@QTYO - D@QTYP` |
| `net_inventory_sy` | `inventory_sy + on_order_sy + partial_received_po` | Derived |
| `days_of_inventory` | `inventory_sy / avg_daily_sales_sy` (inf when no sales) | Derived |
| `inventory_age_days` | Σ(inventory_sy × age_days) / Σ(inventory_sy) | `ROLLS.RLRCTD` |
| `days_since_last_sale` | `today - max(order_entry_date)` | `_ORDERS.ORDER_ENTRY_DATE_YYYYMMDD` |
| `fill_rate` | `1 - (backorder_count / orders_count)` | Derived, clamped 0–1 |
| `stock_turn` | `(avg_daily_sales_sy × 365) / inventory_sy` | Derived |
| `sku_rating` | A/B/C/D quartile bucket by `orders_count` | Derived |
| `runout_risk` | Boolean: inventory will run out before reorder arrives | Derived (lead time + avg daily sales vs inventory) |
| `actual_ship_date` | `INVOICE_SHIP_DATE` if invoiced, else `ORDER_SHIP_DATE` | `_ORDERS` |

---

## 7. Overview Tab — Field Definitions

The Overview tab presents a dashboard-level summary and a per-SKU table.

### Summary KPI Cards (from `summary` dict)

| KPI Card | Metric | Calculation |
|---|---|---|
| **Stock Turn** | `summary["stock_turn"]` | `(Σ avg_daily_sales_sy × 365) / Σ inventory_sy` across all SKUs |
| **Fill Rate** | `summary["fill_rate"]` | `1 - (Σ backorder_count / Σ orders_count)` across all SKUs |
| **Days of Inventory** | `summary["days_of_inventory"]` | Median of per-SKU `days_of_inventory` values |
| **Aging SKUs** | `summary["aging_bad_sku_count"]` | Count of SKUs where `days_since_last_sale >= 540` (18 months) |
| **Runout Risk** | `summary["runout_sku_count"]` | Count of SKUs where `runout_risk = True` |
| **Total SKUs** | `summary["total_skus"]` | Count of all SKUs in current filter scope |

### Sidebar Filters (applied globally across all tabs)
- **Cost Centers** (multiselect) → filters `ITEM.ICCTR`; cost centers starting with `'1'` are always excluded
- **Suppliers** (multiselect) → filters `ITEM.ISUPP#`
- **Price Classes** (multiselect) → filters `ITEM.IPRCCD`
- **SKU Rating** (multiselect A/B/C/D) → filters `sku_rating`
- **Search SKU** (text) → substring match on `sku`
- **Date Range**: Fixed `2025-08-04` through today (not user-adjustable in the Overview)

### Per-SKU Table (`sku_metrics` DataFrame) — Overview Tab Columns

| Display Column | Internal Field | Description |
|---|---|---|
| SKU | `sku` | Base SKU identifier |
| Description | `sku_description` | `ITEM.INAME` |
| Price Class | `price_class_desc` | `PRICE.$DESC` |
| Cost Center | `cost_center` | `ITEM.ICCTR` |
| Rating | `sku_rating` | A/B/C/D quartile based on `orders_count` |
| Inventory (SY) | `inventory_sy` | Available warehouse inventory in SY from ROLLS |
| On Order (SY) | `on_order_sy` | Open PO quantity in SY (ACCOUNT#I=1 lines) |
| Pending PO | `po_pending_qty` | OPENPO_D net qty (ordered − posted), in SY |
| Net Inventory | `net_inventory_sy` | `inventory_sy + on_order_sy + partial_received_po` |
| Avg Daily Sales (SY) | `avg_daily_sales_sy` | `total_quantity_sy / days_in_range` |
| Orders | `orders_count` | Distinct order-line count |
| Backorders | `backorder_count` | Distinct backorder lines (status `B` or `R`) |
| BO Qty (SY) | `backorder_qty_sy` | Sum of SY quantity with status `'B'` only |
| Days of Inventory | `days_of_inventory` | `inventory_sy / avg_daily_sales_sy` |
| Inventory Age (days) | `inventory_age_days` | Weighted average roll age by SY quantity |
| Runout Risk | `runout_risk` | Boolean flag |
| Days Since Last Sale | `days_since_last_sale` | Calendar days since last `order_entry_date` |

### Details Dialog (drill-down for SKU / Supplier / Price Class)
Triggered from sidebar buttons or SKU table rows. Shows:
- **Total Inventory (SY)** metric card
- **Weekly Sales chart** (SKU vs its price class, dual Y-axis)
- **Backorders table**: order_number, quantity_sy, actual_ship_date (status `'B'` only)
- **Purchase Orders table**: order_number, quantity_sy, eta_date

---

## 8. Stock Turn Tab — Field Definitions

The Stock Turn tab is a dedicated per-SKU turn/fill report with its own independent date range picker.

### Date Range Controls
- **Start date** (`stock_turn_start_date`): defaults to `2025-08-04`
- **End date** (`stock_turn_end_date`): defaults to today
- **Use last full month for MTD** checkbox: when checked, MTD period = previous complete calendar month; when unchecked, MTD = current month through selected end date

### Computed Date Windows
| Window | Definition |
|---|---|
| **YTD range** | `stock_start` → `stock_end` (user selected) |
| **MTD range (normal)** | First day of `stock_end` month → `stock_end` |
| **MTD range (full-month mode)** | First → last day of the month prior to `stock_end` |

### Report Table Columns (`report` DataFrame)

| Display Column | Internal Field | Formula / Source |
|---|---|---|
| Price Class | `price_class_desc` | `PRICE.$DESC` via ITEM.IPRCCD join |
| SKU | `sku` | Base SKU identifier |
| Description | `sku_description` | `ITEM.INAME` |
| Rating | `sku_rating` | A/B/C/D recalculated from `orders_count` within the YTD date range |
| Units YTD (SY) | `units_ytd_sy` | Sum of `quantity_sy` for orders with `order_entry_date` in YTD range |
| Units MTD (SY) | `units_mtd_sy` | Sum of `quantity_sy` for orders with `order_entry_date` in MTD range |
| Inventory (SY) | `inventory_sy` | Current warehouse inventory from ROLLS (same as Overview) |
| On Order (SY) | `on_order_sy` | Open PO quantity in SY from _ORDERS (ACCOUNT#I=1) |
| YTD Turn | `ytd_turn` | `(avg_daily_sales_sy × 365) / inventory_sy` — avg daily is `units_ytd_sy / days_in_range` |
| MTD Turn | `mtd_turn` | `(units_mtd_sy × (days_in_month / elapsed_days) × 12) / inventory_sy` — projects MTD to annual |
| Fill Rate (YTD) | `fill_rate` | `1 - (backorder_count / orders_count)` for distinct lines in YTD range |
| Fill Rate (MTD) | `mtd_fill_rate` | `1 - (backorder_count_mtd / orders_count_mtd)` for distinct lines in MTD range |
| Days of Inventory | `days_of_inventory` | `inventory_sy / avg_daily_sales_sy` (recalculated for YTD range) |
| Inventory Age (days) | `inventory_age_days` | Weighted average roll age (same as Overview) |

### Stock Turn Formulas — Detail

```
avg_daily_sales_sy = units_ytd_sy / days_in_range
days_in_range      = (stock_end - stock_start).days + 1  (minimum 1)

ytd_turn  = (avg_daily_sales_sy × 365) / inventory_sy
mtd_turn  = (units_mtd_sy × (days_in_month / elapsed_days) × 12) / inventory_sy

fill_rate     = 1 - (backorder_count     / orders_count)      [clamped 0–1]
mtd_fill_rate = 1 - (backorder_count_mtd / orders_count_mtd)  [clamped 0–1]

days_of_inventory = inventory_sy / avg_daily_sales_sy
```

- Both turn metrics → `0` when `inventory_sy = 0`
- `mtd_turn` uses `elapsed_days = days_in_month` when **full-month mode** is on

### Stock Turn Target
- Configurable per cost center via `%APPDATA%\PurchaseOrderBot\config\stockturn_targets.json`
- Default global target: `4.0` (stored in `AppConfig.stockturn_target`)
- Used for highlighting under-performing SKUs

### Default Sort Order
1. `price_class_desc` ascending
2. `units_mtd_sy` ascending (lowest sellers first)
3. `sku` ascending

### PDF Export Columns (Stock Turn PDF)
```
Price Class | SKU | Desc | Inv(SY) | On Order(SY) | YTD Units | MTD Units |
YTD Turn | MTD Turn | Fill% | Fill%_MTD | DOI
```
Plus a group-level summary row per price class showing totals and group fill rate.

---

## 9. Key Business Rules Summary

| Rule | Detail |
|---|---|
| Active inventory items | `ITEM.IINVEN = 'Y'` |
| Exclude discontinued items | `ITEM.IDISCD` is null / blank / `'0'` |
| Dropped items | `ITEM.IPOL1` or `IPOL2` or `IPOL3 = 'DI'` AND `IDISCD > 0` |
| Sales orders (customer) | `_ORDERS.ACCOUNT#I > 1` |
| Purchase orders (warehouse) | `_ORDERS.ACCOUNT#I = 1` |
| Open Orders filter | `SUPPLIER# = '001'` AND `ACCOUNT#I != 1` |
| Backorder status | `DETAIL_LINE_STATUS` exactly `'B'` or `'R'` (case-insensitive) |
| Strict backorder qty | Only `'B'` status (not `'R'`) for quantity-level backorder metrics |
| Remnant rolls excluded | `ROLLS.RLOC1 = 'REM'` → excluded |
| Inactive roll status | `ROLLS.RCODE@ = '#'` or contains `'I'` → excluded |
| Valid PO number | `_ORDERS.ORDER# > 0` (numeric) |
| Exclude cost centers starting with '1' | Applied in `_resolve_cost_centers()` |
| Future-dated orders | Excluded (order_entry_date > today) |
| Non-positive quantities | Excluded from all metrics |
| SKU alias resolution | If `ITEM.IIXREF` is set, map SKU → IIXREF as base before any groupby |
| OPENPO_D supplier exclusion | `D@SUPP = '001'` excluded from pending POs |

---

## 10. AppConfig Defaults

```python
connection_string:      (resolved from env/file/config_local)
stockturn_target:       4.0       # default stock turn target
default_cost_centers:   ["010"]
default_date_months:    18        # historical window for demand
rating_buckets:         (0.25, 0.50, 0.75)  # quartile thresholds for A/B/C/D
cache_ttl_seconds:      360       # 6 minutes — how long SQLAlchemy query results are cached
```

---

## 11. File Structure Reference

```
app/
  config.py              — AppConfig dataclass, connection string resolution
  data/
    db.py                — SQLAlchemy engine, read_dataframe(), validate_connection()
    queries.py           — All raw SQL strings (ORDERS_BASE, ITEMS, ROLLS, etc.)
    loaders.py           — Data loading functions with filter/param injection
    stockturn_store.py   — Per-cost-center stock turn target persistence (JSON)
    seasonality_store.py — Monthly seasonality % per cost center (JSON)
    launch_store.py      — Price class launch date tracking
    history_store.py     — Metrics snapshot history (CSV)
    backorder_store.py   — Backorder persistence
  services/
    metrics_service.py   — compute_dashboard_data(), all KPI calculations
    sku_rating.py        — assign_sku_ratings() A/B/C/D quartile logic
    reorder.py           — Reorder point / runout risk calculations
  ui/
    dashboard.py         — Streamlit UI (all tabs)
config_local.py          — Local connection string override (not committed)
```
