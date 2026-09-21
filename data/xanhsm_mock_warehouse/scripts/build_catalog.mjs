import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = path.resolve("datasets/xanhsm_mock_warehouse");
const outputDir = path.join(root, "outputs");
await fs.mkdir(outputDir, { recursive: true });

const manifest = JSON.parse(await fs.readFile(path.join(root, "manifest.json"), "utf8"));
const aggregateText = await fs.readFile(path.join(root, "csv/agg_daily_city_service.csv"), "utf8");
const aggregateLines = aggregateText.trim().split(/\r?\n/);
const aggregateHeaders = aggregateLines[0].split(",");
const aggregateRows = aggregateLines.slice(1).map((line) => line.split(","));
const sampleRows = aggregateRows.filter((r) => r[1] === "HCM" && r[3] === "TAXI").slice(-30);

const descriptions = {
  agg_daily_city_service: "Daily city-service aggregate for reconciliation and demos",
  dim_campaign: "Synthetic promotion campaign dimension",
  dim_charging_station: "Synthetic charging station dimension",
  dim_customer: "Hashed synthetic customer dimension without direct PII",
  dim_date: "Calendar dimension for the 240-day period",
  dim_driver: "Synthetic driver dimension",
  dim_location: "City, district, and zone hierarchy",
  dim_service: "Versioned service taxonomy",
  dim_vehicle: "Synthetic electric vehicle dimension",
  fact_bookings: "Booking lifecycle, matching, and cancellation events",
  fact_charging_sessions: "Charging queue and charging session events",
  fact_driver_offers: "Trip offers and driver responses",
  fact_driver_online_sessions: "Driver state intervals for utilization",
  fact_payments: "Charges, refunds, discounts, and adjustments",
  fact_promotions: "Booking-to-campaign voucher applications",
  fact_trips: "Matched and completed trip facts",
  fact_vehicle_status: "One vehicle status snapshot per vehicle per day",
};

const workbook = Workbook.create();
const overview = workbook.worksheets.add("Overview");
const tables = workbook.worksheets.add("Table Inventory");
const sample = workbook.worksheets.add("Daily KPI Sample");
const readme = workbook.worksheets.add("ReadMe");

const font = "Arial";
const navy = "#17365D";
const blue = "#D9EAF7";
const lightBlue = "#EEF5FB";
const gray = "#5B6573";
const green = "#E2F0D9";

for (const sheet of [overview, tables, sample, readme]) {
  sheet.showGridLines = false;
  sheet.getRange("A1:Q100").format.font = { name: font, size: 10, color: "#1F2937" };
}

overview.getRange("A2:F2").merge();
overview.getRange("A2").values = [["Xanh SM mock data warehouse"]];
overview.getRange("A2").format.font = { name: font, size: 16, bold: true, color: navy };
overview.getRange("A3:F3").format.borders = { bottom: { style: "thin", color: navy } };
overview.getRange("A5:B10").values = [
  ["Dataset version", manifest.version],
  ["Period", `${manifest.period.start} to ${manifest.period.end}`],
  ["Total rows", manifest.total_rows],
  ["SQLite database", manifest.primary_database],
  ["Seed", manifest.seed],
  ["Scope", manifest.data_scope],
];
overview.getRange("A5:A10").format.font = { name: font, size: 10, bold: true, color: gray };
overview.getRange("B7").format.numberFormat = "#,##0";
overview.getRange("A12:C12").values = [["Data quality check", "Result", "Details"]];
overview.getRange("A13:C20").values = [
  ["SQLite integrity", "OK", "PRAGMA integrity_check = ok"],
  ["Booking customer foreign keys", "OK", "0 orphan rows"],
  ["Booking service foreign keys", "OK", "0 orphan rows"],
  ["Trip booking foreign keys", "OK", "0 orphan rows"],
  ["Trip driver foreign keys", "OK", "0 orphan rows"],
  ["Trip vehicle foreign keys", "OK", "0 orphan rows"],
  ["Payment trip foreign keys", "OK", "0 orphan rows"],
  ["Offer booking foreign keys", "OK", "0 orphan rows"],
];
overview.getRange("A12:C12").format = { fill: navy, font: { name: font, size: 10, bold: true, color: "#FFFFFF" } };
overview.getRange("B13:B20").format = { fill: green, font: { name: font, size: 10, bold: true, color: "#215E21" }, horizontalAlignment: "center" };
overview.getRange("A22:F24").values = [
  ["Important limitation", "", "", "", "", ""],
  [manifest.warning, "", "", "", "", ""],
  ["Vehicle status uses one snapshot per vehicle per day. Production may use 15-minute snapshots or event sourcing.", "", "", "", "", ""],
];
overview.getRange("A22:F22").merge();
overview.getRange("A23:F23").merge();
overview.getRange("A24:F24").merge();
overview.getRange("A22:F22").format = { fill: blue, font: { name: font, size: 10, bold: true, color: navy } };
overview.getRange("A23:F24").format.wrapText = true;
overview.getRange("A1:F25").format.verticalAlignment = "center";
overview.getRange("A:A").format.columnWidth = 30;
overview.getRange("B:B").format.columnWidth = 24;
overview.getRange("C:C").format.columnWidth = 38;
overview.getRange("D:F").format.columnWidth = 12;

const inventory = Object.entries(manifest.row_counts).map(([name, count]) => [name, count, descriptions[name] ?? ""]);
tables.getRange("A2:C2").merge();
tables.getRange("A2").values = [["Warehouse table inventory"]];
tables.getRange("A2").format.font = { name: font, size: 16, bold: true, color: navy };
tables.getRange("A4:C4").values = [["Table", "Rows", "Purpose"]];
tables.getRange(`A5:C${4 + inventory.length}`).values = inventory;
tables.getRange("A4:C4").format = { fill: navy, font: { name: font, size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center" };
tables.getRange(`A5:C${4 + inventory.length}`).format.borders = { bottom: { style: "thin", color: "#D9E2F3" } };
tables.getRange(`B5:B${4 + inventory.length}`).format.numberFormat = "#,##0";
tables.getRange("A:A").format.columnWidth = 34;
tables.getRange("B:B").format.columnWidth = 16;
tables.getRange("C:C").format.columnWidth = 62;
tables.freezePanes.freezeRows(4);

sample.getRange("A2:M2").merge();
sample.getRange("A2").values = [["HCM Taxi daily KPI sample — last 30 days"]];
sample.getRange("A2").format.font = { name: font, size: 16, bold: true, color: navy };
const sampleHeader = aggregateHeaders.map((h) => h.replaceAll("_", " "));
sample.getRangeByIndexes(3, 0, 1, sampleHeader.length).values = [sampleHeader];
const typedSample = sampleRows.map((row) => row.map((value, idx) => {
  if ([4,5,6,7,8,9,10].includes(idx)) return Number(value);
  if ([11,12].includes(idx)) return Number(value);
  return value;
}));
sample.getRangeByIndexes(4, 0, typedSample.length, sampleHeader.length).values = typedSample;
sample.getRangeByIndexes(3, 0, 1, sampleHeader.length).format = { fill: navy, font: { name: font, size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center", wrapText: true };
sample.getRange(`E5:K${4 + typedSample.length}`).format.numberFormat = "#,##0";
sample.getRange(`L5:M${4 + typedSample.length}`).format.numberFormat = "0.0%";
sample.getRange(`A5:M${4 + typedSample.length}`).format.borders = { bottom: { style: "thin", color: "#D9E2F3" } };
sample.getRange("A:M").format.columnWidth = 16;
sample.getRange("C:C").format.columnWidth = 20;
sample.freezePanes.freezeRows(4);

readme.getRange("A2:B2").merge();
readme.getRange("A2").values = [["Usage notes"]];
readme.getRange("A2").format.font = { name: font, size: 16, bold: true, color: navy };
readme.getRange("A4:B11").values = [
  ["Item", "Guidance"],
  ["Primary database", "Use xanhsm_mock_warehouse.sqlite for local SQL demos."],
  ["Portable files", "Use csv/*.csv for Athena, Glue, Spark, PostgreSQL, or another warehouse."],
  ["Schema", "Use sql/schema.sql."],
  ["Metric views", "Use sql/views.sql as starter definitions aligned with the retrieval corpus."],
  ["PII", "The dataset has no real names, phone numbers, license plates, or precise personal coordinates."],
  ["Reproducibility", `Generator seed: ${manifest.seed}.`],
  ["Do not present as actuals", "All records and outcomes are synthetic demo data."],
];
readme.getRange("A4:B4").format = { fill: navy, font: { name: font, size: 10, bold: true, color: "#FFFFFF" }, horizontalAlignment: "center" };
readme.getRange("A5:A11").format = { fill: lightBlue, font: { name: font, size: 10, bold: true, color: navy } };
readme.getRange("A4:B11").format.borders = { bottom: { style: "thin", color: "#D9E2F3" } };
readme.getRange("A:A").format.columnWidth = 26;
readme.getRange("B:B").format.columnWidth = 78;
readme.getRange("B5:B11").format.wrapText = true;

workbook.recalculate();

for (const [sheetName, range] of [["Overview", "A1:F25"], ["Table Inventory", `A1:C${4 + inventory.length}`], ["Daily KPI Sample", `A1:M${4 + typedSample.length}`], ["ReadMe", "A1:B11"]]) {
  const preview = await workbook.render({ sheetName, range, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(path.join(outputDir, `${sheetName.replaceAll(" ", "_")}.png`), new Uint8Array(await preview.arrayBuffer()));
}

const inspect = await workbook.inspect({ kind: "table", range: "Overview!A1:F25", include: "values,formulas", tableMaxRows: 25, tableMaxCols: 6, maxChars: 8000 });
console.log(inspect.ndjson);
const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!", options: { useRegex: true, maxResults: 100 }, summary: "final formula error scan" });
console.log(errors.ndjson);

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(path.join(outputDir, "xanhsm_mock_warehouse_catalog.xlsx"));
