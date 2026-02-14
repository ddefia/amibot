"""Analyze trader data from Polymarket BTC 5-min markets.

Handles multiple data formats found in trader research folders:
- CSV files: trade logs, P&L reports, order histories
- PDF files: strategy docs, account statements, trade summaries
- Screenshots (PNG/JPG/WEBP): trade UIs, P&L charts, order book snapshots
- JSON files: API exports, trade records
- Excel files: spreadsheets with trade data
- Text files: notes, strategy descriptions

Extracts actionable patterns:
- Entry timing within 5-min intervals
- Position sizing patterns
- Win rate by time of day, price conditions, volatility
- Edge estimation and strategy classification
"""

import io
import os
import re
import logging
from pathlib import Path

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# Optional imports — degrade gracefully
try:
    import pdfplumber
    HAS_PDF = True
except ImportError:
    HAS_PDF = False
    logger.warning("pdfplumber not installed — PDF parsing disabled")

try:
    from PIL import Image
    import pytesseract
    HAS_OCR = True
except ImportError:
    HAS_OCR = False
    logger.warning("Pillow/pytesseract not installed — OCR disabled")


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}
PDF_EXTENSIONS = {".pdf"}
TABLE_EXTENSIONS = {".csv", ".tsv", ".json", ".xlsx", ".xls"}
TEXT_EXTENSIONS = {".txt", ".md", ".log"}


class FileInventory:
    """Catalogs all files in a trader folder by type."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.csvs: list[Path] = []
        self.jsons: list[Path] = []
        self.excels: list[Path] = []
        self.pdfs: list[Path] = []
        self.images: list[Path] = []
        self.texts: list[Path] = []
        self.unknown: list[Path] = []
        self._scan()

    def _scan(self):
        for f in sorted(self.folder.rglob("*")):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext == ".csv" or ext == ".tsv":
                self.csvs.append(f)
            elif ext == ".json":
                self.jsons.append(f)
            elif ext in {".xlsx", ".xls"}:
                self.excels.append(f)
            elif ext in PDF_EXTENSIONS:
                self.pdfs.append(f)
            elif ext in IMAGE_EXTENSIONS:
                self.images.append(f)
            elif ext in TEXT_EXTENSIONS:
                self.texts.append(f)
            else:
                self.unknown.append(f)

    @property
    def total(self) -> int:
        return (
            len(self.csvs) + len(self.jsons) + len(self.excels)
            + len(self.pdfs) + len(self.images) + len(self.texts)
            + len(self.unknown)
        )

    def summary(self) -> dict:
        return {
            "csv": len(self.csvs),
            "json": len(self.jsons),
            "excel": len(self.excels),
            "pdf": len(self.pdfs),
            "images": len(self.images),
            "text": len(self.texts),
            "unknown": len(self.unknown),
            "total": self.total,
        }


class PDFExtractor:
    """Extract tables and text from PDF trade documents."""

    @staticmethod
    def extract_tables(pdf_path: Path) -> list[pd.DataFrame]:
        """Extract all tables from a PDF file."""
        if not HAS_PDF:
            logger.warning("pdfplumber not available, skipping %s", pdf_path.name)
            return []

        frames = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    tables = page.extract_tables()
                    for table in tables:
                        if not table or len(table) < 2:
                            continue
                        # First row as header
                        header = [str(c).strip() if c else f"col_{i}" for i, c in enumerate(table[0])]
                        rows = table[1:]
                        df = pd.DataFrame(rows, columns=header)
                        # Drop empty rows/cols
                        df = df.dropna(how="all").dropna(axis=1, how="all")
                        if not df.empty:
                            frames.append(df)
                            logger.info(
                                "Extracted table (%d rows) from %s",
                                len(df), pdf_path.name,
                            )
        except Exception:
            logger.exception("Failed to extract tables from %s", pdf_path.name)

        return frames

    @staticmethod
    def extract_text(pdf_path: Path) -> str:
        """Extract raw text from a PDF file."""
        if not HAS_PDF:
            return ""

        text_parts = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        text_parts.append(text)
        except Exception:
            logger.exception("Failed to extract text from %s", pdf_path.name)

        return "\n".join(text_parts)

    @staticmethod
    def extract_trades_from_text(text: str) -> pd.DataFrame | None:
        """Parse trade data from raw PDF text using regex patterns."""
        # Common patterns in Polymarket trade exports
        patterns = {
            # Match lines like: "2024-01-15 14:32:05  BUY  Up  $0.55  100  +$45.00"
            "full_trade": re.compile(
                r"(\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}(?::\d{2})?)"  # timestamp
                r"\s+(BUY|SELL|buy|sell)"  # side
                r"\s+(Up|Down|YES|NO|up|down|yes|no)"  # outcome
                r"\s+\$?([\d.]+)"  # price
                r"\s+([\d.]+)"  # size
                r"\s+[+-]?\$?([\d.]+)",  # pnl
                re.IGNORECASE,
            ),
            # Match lines like: "BUY Up 0.52 150 shares"
            "simple_trade": re.compile(
                r"(BUY|SELL|buy|sell)"
                r"\s+(Up|Down|YES|NO|up|down|yes|no)"
                r"\s+\$?([\d.]+)"
                r"\s+([\d.]+)",
                re.IGNORECASE,
            ),
            # Match lines like: "Won $45.00" or "Lost $12.50"
            "outcome_line": re.compile(
                r"(Won|Lost|Win|Loss|Profit|Loss)"
                r"\s+\$?([\d.]+)",
                re.IGNORECASE,
            ),
        }

        rows = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue

            match = patterns["full_trade"].search(line)
            if match:
                rows.append({
                    "timestamp": match.group(1),
                    "side": match.group(2).upper(),
                    "outcome_type": match.group(3).upper(),
                    "entry_price": float(match.group(4)),
                    "size": float(match.group(5)),
                    "pnl": float(match.group(6)),
                })
                continue

            match = patterns["simple_trade"].search(line)
            if match:
                rows.append({
                    "side": match.group(1).upper(),
                    "outcome_type": match.group(2).upper(),
                    "entry_price": float(match.group(3)),
                    "size": float(match.group(4)),
                })

        if not rows:
            return None

        return pd.DataFrame(rows)


class ImageExtractor:
    """Extract trade data from screenshots using OCR."""

    @staticmethod
    def extract_text(image_path: Path) -> str:
        """Run OCR on a screenshot and return raw text."""
        if not HAS_OCR:
            logger.warning("OCR not available, skipping %s", image_path.name)
            return ""

        try:
            img = Image.open(image_path)
            text = pytesseract.image_to_string(img)
            if text.strip():
                logger.info(
                    "OCR extracted %d chars from %s",
                    len(text), image_path.name,
                )
            return text
        except Exception:
            logger.exception("OCR failed for %s", image_path.name)
            return ""

    @staticmethod
    def extract_numbers(text: str) -> dict:
        """Pull key numbers from OCR'd screenshot text."""
        results = {}

        # Look for P&L values like "+$1,234.56" or "-$500"
        pnl_matches = re.findall(r"[+-]?\$[\d,]+\.?\d*", text)
        if pnl_matches:
            results["pnl_values"] = [
                float(m.replace("$", "").replace(",", ""))
                for m in pnl_matches
            ]

        # Look for percentages like "85.2%" (win rates)
        pct_matches = re.findall(r"(\d+\.?\d*)%", text)
        if pct_matches:
            results["percentages"] = [float(m) for m in pct_matches]

        # Look for share counts like "150 shares"
        share_matches = re.findall(r"(\d+)\s*(?:shares|contracts|lots)", text, re.IGNORECASE)
        if share_matches:
            results["share_counts"] = [int(m) for m in share_matches]

        # Look for prices like "$0.55" or "0.55"
        price_matches = re.findall(r"\$?(0\.\d+)", text)
        if price_matches:
            results["prices"] = [float(m) for m in price_matches]

        # Look for win/loss counts
        wl_match = re.search(r"(\d+)\s*(?:wins?|W)\s*[/,]\s*(\d+)\s*(?:loss|losses|L)", text, re.IGNORECASE)
        if wl_match:
            results["wins"] = int(wl_match.group(1))
            results["losses"] = int(wl_match.group(2))

        return results

    @staticmethod
    def get_image_info(image_path: Path) -> dict:
        """Get basic image metadata without OCR."""
        try:
            img = Image.open(image_path)
            return {
                "filename": image_path.name,
                "size": img.size,
                "format": img.format,
                "path": str(image_path),
            }
        except Exception:
            return {"filename": image_path.name, "path": str(image_path)}


class TraderAnalyzer:
    """Load and analyze individual trader data to reverse-engineer strategies.

    Handles CSVs, PDFs, screenshots, JSON, Excel, and text files."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.traders: dict[str, pd.DataFrame] = {}
        self.raw_texts: dict[str, list[str]] = {}  # PDF/OCR text per trader
        self.image_data: dict[str, list[dict]] = {}  # extracted screenshot data
        self.inventories: dict[str, FileInventory] = {}

    def load_all(self) -> dict[str, pd.DataFrame]:
        """Load all trader data from subdirectories.

        Handles nested structures — scans recursively. Also supports
        flat structure where all 5 folders sit directly under data_dir.
        """
        if not self.data_dir.exists():
            logger.warning("Data directory %s does not exist", self.data_dir)
            return {}

        folders = []
        for item in sorted(self.data_dir.iterdir()):
            if item.is_dir():
                folders.append(item)

        # If no subdirectories, treat data_dir itself as a single trader
        if not folders:
            folders = [self.data_dir]

        for folder in folders:
            trader_name = folder.name
            inventory = FileInventory(folder)
            self.inventories[trader_name] = inventory

            logger.info(
                "Trader '%s': %s", trader_name, inventory.summary()
            )

            # Load structured data (CSVs, JSON, Excel)
            df = self._load_structured_data(inventory)

            # Load PDF tables and text
            pdf_df, pdf_texts = self._load_pdfs(inventory)
            if pdf_texts:
                self.raw_texts.setdefault(trader_name, []).extend(pdf_texts)

            # Load screenshot data via OCR
            img_data, img_texts = self._load_images(inventory)
            if img_data:
                self.image_data[trader_name] = img_data
            if img_texts:
                self.raw_texts.setdefault(trader_name, []).extend(img_texts)

            # Load text files
            txt_texts = self._load_text_files(inventory)
            if txt_texts:
                self.raw_texts.setdefault(trader_name, []).extend(txt_texts)

            # Combine all tabular data
            frames = []
            if df is not None and not df.empty:
                frames.append(df)
            if pdf_df is not None and not pdf_df.empty:
                frames.append(pdf_df)

            if frames:
                combined = pd.concat(frames, ignore_index=True)
                combined = self._normalize_columns(combined)
                self.traders[trader_name] = combined
                logger.info(
                    "Loaded %d trades for trader '%s'", len(combined), trader_name
                )
            elif self.raw_texts.get(trader_name) or self.image_data.get(trader_name):
                # No tabular data but we have text/images — create empty df with notes
                self.traders[trader_name] = pd.DataFrame()
                logger.info(
                    "Trader '%s': no tabular data, but has %d text excerpts and %d images",
                    trader_name,
                    len(self.raw_texts.get(trader_name, [])),
                    len(self.image_data.get(trader_name, [])),
                )

        logger.info("Loaded %d traders total", len(self.traders))
        return self.traders

    def _load_structured_data(self, inv: FileInventory) -> pd.DataFrame | None:
        """Load CSVs, JSONs, and Excel files."""
        frames = []

        for f in inv.csvs:
            try:
                # Try multiple encodings and separators
                for encoding in ["utf-8", "latin-1", "cp1252"]:
                    try:
                        df = pd.read_csv(f, encoding=encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    df = pd.read_csv(f, encoding="utf-8", errors="replace")

                if not df.empty:
                    df["_source_file"] = f.name
                    frames.append(df)
                    logger.info("Loaded CSV %s (%d rows)", f.name, len(df))
            except Exception:
                logger.warning("Failed to load CSV %s", f.name)

        for f in inv.jsons:
            try:
                df = pd.read_json(f)
                if not df.empty:
                    df["_source_file"] = f.name
                    frames.append(df)
                    logger.info("Loaded JSON %s (%d rows)", f.name, len(df))
            except Exception:
                # Might be a non-tabular JSON — read as text
                try:
                    text = f.read_text(errors="replace")
                    logger.info("JSON %s is non-tabular, stored as text", f.name)
                except Exception:
                    logger.warning("Failed to load JSON %s", f.name)

        for f in inv.excels:
            try:
                df = pd.read_excel(f)
                if not df.empty:
                    df["_source_file"] = f.name
                    frames.append(df)
                    logger.info("Loaded Excel %s (%d rows)", f.name, len(df))
            except Exception:
                logger.warning("Failed to load Excel %s", f.name)

        if not frames:
            return None

        return pd.concat(frames, ignore_index=True)

    def _load_pdfs(self, inv: FileInventory) -> tuple[pd.DataFrame | None, list[str]]:
        """Extract tables and text from all PDFs."""
        frames = []
        texts = []

        for f in inv.pdfs:
            # Extract tables
            tables = PDFExtractor.extract_tables(f)
            for tbl in tables:
                tbl["_source_file"] = f.name
                frames.append(tbl)

            # Extract text
            text = PDFExtractor.extract_text(f)
            if text.strip():
                texts.append(f"[PDF: {f.name}]\n{text}")

                # Try to parse trades from the text too
                text_df = PDFExtractor.extract_trades_from_text(text)
                if text_df is not None and not text_df.empty:
                    text_df["_source_file"] = f"{f.name} (text-parsed)"
                    frames.append(text_df)

        combined = pd.concat(frames, ignore_index=True) if frames else None
        return combined, texts

    def _load_images(self, inv: FileInventory) -> tuple[list[dict], list[str]]:
        """Extract data from screenshots via OCR."""
        all_data = []
        texts = []

        for f in inv.images:
            info = ImageExtractor.get_image_info(f)

            # Run OCR
            text = ImageExtractor.extract_text(f)
            if text.strip():
                texts.append(f"[Screenshot: {f.name}]\n{text}")

                # Extract numbers from OCR text
                numbers = ImageExtractor.extract_numbers(text)
                info["ocr_text"] = text[:500]  # first 500 chars
                info["extracted_numbers"] = numbers
            else:
                info["ocr_text"] = ""
                info["extracted_numbers"] = {}

            all_data.append(info)

        return all_data, texts

    def _load_text_files(self, inv: FileInventory) -> list[str]:
        """Load plain text files (notes, strategy descriptions)."""
        texts = []
        for f in inv.texts:
            try:
                text = f.read_text(errors="replace")
                if text.strip():
                    texts.append(f"[Text: {f.name}]\n{text}")
            except Exception:
                logger.warning("Failed to read text file %s", f.name)
        return texts

    def _normalize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalize column names to a standard format.
        Handles messy, inconsistent column names from various sources."""
        col_map = {}
        for col in df.columns:
            if col.startswith("_"):
                continue  # skip internal columns
            lower = col.lower().strip().replace("_", " ").replace("-", " ")

            if any(kw in lower for kw in ["time", "date", "timestamp", "created", "executed"]):
                col_map[col] = "timestamp"
            elif ("price" in lower and "entry" in lower) or lower in ["buy price", "fill price", "exec price"]:
                col_map[col] = "entry_price"
            elif "price" in lower and "exit" in lower:
                col_map[col] = "exit_price"
            elif lower in ["price", "avg price", "fill", "cost"]:
                col_map[col] = "entry_price"
            elif any(kw in lower for kw in ["side", "direction", "type", "action"]):
                col_map[col] = "side"
            elif any(kw in lower for kw in ["size", "amount", "quantity", "qty", "shares", "contracts", "volume"]):
                col_map[col] = "size"
            elif any(kw in lower for kw in ["pnl", "profit", "p&l", "gain", "return", "net"]):
                col_map[col] = "pnl"
            elif any(kw in lower for kw in ["outcome", "result", "won", "status", "resolved"]):
                col_map[col] = "outcome"
            elif any(kw in lower for kw in ["market", "slug", "event", "question"]):
                col_map[col] = "market"
            elif any(kw in lower for kw in ["token", "asset"]):
                col_map[col] = "token_id"
            elif any(kw in lower for kw in ["fee", "commission"]):
                col_map[col] = "fee"

        df = df.rename(columns=col_map)

        # Parse timestamps
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            valid = df["timestamp"].notna()
            if valid.any():
                df = df.sort_values("timestamp").reset_index(drop=True)

        # Coerce numeric columns
        for col in ["entry_price", "exit_price", "size", "pnl", "fee"]:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace(r"[$,]", "", regex=True),
                    errors="coerce",
                )

        return df

    def get_inventory(self, name: str) -> dict:
        """Get file inventory summary for a trader."""
        if name in self.inventories:
            return self.inventories[name].summary()
        return {}

    def get_raw_text(self, name: str) -> str:
        """Get all extracted text (PDFs + OCR + text files) for a trader."""
        texts = self.raw_texts.get(name, [])
        return "\n\n".join(texts)

    def get_screenshot_data(self, name: str) -> list[dict]:
        """Get extracted data from screenshots for a trader."""
        return self.image_data.get(name, [])

    def analyze_trader(self, name: str) -> dict:
        """Generate a full analysis of a single trader's behavior."""
        if name not in self.traders:
            return {"error": f"Trader '{name}' not found"}

        df = self.traders[name]
        analysis = {
            "name": name,
            "total_trades": len(df),
            "files": self.get_inventory(name),
        }

        if df.empty:
            # Try to extract stats from text/image data
            analysis["data_source"] = "text/screenshots only"
            analysis.update(self._analyze_from_text(name))
            return analysis

        analysis["data_source"] = "tabular"

        # Win/loss metrics
        if "outcome" in df.columns:
            wins = df["outcome"].astype(str).str.lower().isin(
                ["win", "won", "true", "1", "yes", "resolved", "success"]
            )
            analysis["wins"] = int(wins.sum())
            analysis["losses"] = int((~wins).sum())
            analysis["win_rate"] = float(wins.mean())
        elif "pnl" in df.columns and df["pnl"].notna().any():
            pnl = df["pnl"].dropna()
            analysis["wins"] = int((pnl > 0).sum())
            analysis["losses"] = int((pnl <= 0).sum())
            analysis["win_rate"] = float((pnl > 0).mean())

        # P&L analysis
        if "pnl" in df.columns and df["pnl"].notna().any():
            pnl = df["pnl"].dropna().astype(float)
            analysis["total_pnl"] = float(pnl.sum())
            analysis["avg_win"] = float(pnl[pnl > 0].mean()) if (pnl > 0).any() else 0
            analysis["avg_loss"] = float(pnl[pnl <= 0].mean()) if (pnl <= 0).any() else 0
            analysis["max_win"] = float(pnl.max())
            analysis["max_loss"] = float(pnl.min())
            analysis["profit_factor"] = (
                float(pnl[pnl > 0].sum() / abs(pnl[pnl < 0].sum()))
                if (pnl < 0).any() and pnl[pnl < 0].sum() != 0
                else float("inf")
            )
            analysis["sharpe"] = float(pnl.mean() / pnl.std()) if pnl.std() > 0 else 0

            # Consecutive win/loss streaks
            signs = (pnl > 0).astype(int)
            streaks = signs.groupby((signs != signs.shift()).cumsum())
            win_streaks = [len(g) for _, g in streaks if g.iloc[0] == 1]
            loss_streaks = [len(g) for _, g in streaks if g.iloc[0] == 0]
            analysis["max_win_streak"] = max(win_streaks) if win_streaks else 0
            analysis["max_loss_streak"] = max(loss_streaks) if loss_streaks else 0

        # Position sizing patterns
        if "size" in df.columns and df["size"].notna().any():
            sizes = df["size"].dropna().astype(float)
            analysis["avg_size"] = float(sizes.mean())
            analysis["median_size"] = float(sizes.median())
            analysis["max_size"] = float(sizes.max())
            analysis["min_size"] = float(sizes.min())
            analysis["size_stddev"] = float(sizes.std())
            # Do they vary size or keep it constant?
            analysis["size_cv"] = float(sizes.std() / sizes.mean()) if sizes.mean() > 0 else 0

        # Entry price patterns
        if "entry_price" in df.columns and df["entry_price"].notna().any():
            prices = df["entry_price"].dropna().astype(float)
            analysis["avg_entry_price"] = float(prices.mean())
            analysis["median_entry_price"] = float(prices.median())
            analysis["pct_entries_below_35c"] = float((prices < 0.35).mean())
            analysis["pct_entries_near_50c"] = float(
                ((prices > 0.40) & (prices < 0.60)).mean()
            )
            analysis["pct_entries_above_65c"] = float((prices > 0.65).mean())

            # Price distribution buckets
            analysis["entry_price_distribution"] = {
                "0-20c": float((prices < 0.20).mean()),
                "20-35c": float(((prices >= 0.20) & (prices < 0.35)).mean()),
                "35-50c": float(((prices >= 0.35) & (prices < 0.50)).mean()),
                "50-65c": float(((prices >= 0.50) & (prices < 0.65)).mean()),
                "65-80c": float(((prices >= 0.65) & (prices < 0.80)).mean()),
                "80c+": float((prices >= 0.80).mean()),
            }

        # Fee analysis
        if "fee" in df.columns and df["fee"].notna().any():
            fees = df["fee"].dropna().astype(float)
            analysis["total_fees"] = float(fees.sum())
            analysis["avg_fee"] = float(fees.mean())
            analysis["pct_zero_fee"] = float((fees == 0).mean())

        # Timing analysis
        if "timestamp" in df.columns and df["timestamp"].notna().any():
            ts = df["timestamp"].dropna()
            analysis["trading_hours"] = self._analyze_timing(ts)
            analysis["date_range"] = {
                "first_trade": str(ts.min()),
                "last_trade": str(ts.max()),
                "trading_days": int(ts.dt.date.nunique()),
            }

            # Interval timing: when during the 5-min window do they trade?
            seconds_in_interval = ts.dt.second + (ts.dt.minute % 5) * 60
            analysis["avg_seconds_into_interval"] = float(seconds_in_interval.mean())
            analysis["entry_timing_distribution"] = {
                "first_60s": float((seconds_in_interval < 60).mean()),
                "60_120s": float(
                    ((seconds_in_interval >= 60) & (seconds_in_interval < 120)).mean()
                ),
                "120_180s": float(
                    ((seconds_in_interval >= 120) & (seconds_in_interval < 180)).mean()
                ),
                "180_240s": float(
                    ((seconds_in_interval >= 180) & (seconds_in_interval < 240)).mean()
                ),
                "last_60s": float((seconds_in_interval >= 240).mean()),
            }

        # Side preference
        if "side" in df.columns and df["side"].notna().any():
            side_counts = df["side"].astype(str).str.lower().value_counts()
            analysis["side_preference"] = side_counts.to_dict()

        # Market diversity
        if "market" in df.columns and df["market"].notna().any():
            analysis["unique_markets"] = int(df["market"].nunique())

        return analysis

    def _analyze_from_text(self, name: str) -> dict:
        """Extract whatever stats we can from raw text (PDFs/OCR/notes)."""
        results = {}
        all_text = self.get_raw_text(name)

        if not all_text:
            return results

        # Try to find win rate
        wr_match = re.search(r"win\s*rate[:\s]*(\d+\.?\d*)%", all_text, re.IGNORECASE)
        if wr_match:
            results["win_rate"] = float(wr_match.group(1)) / 100

        # Try to find total P&L
        pnl_match = re.search(r"(?:total|net)\s*(?:p[&/]?l|profit)[:\s]*[+-]?\$?([\d,]+\.?\d*)", all_text, re.IGNORECASE)
        if pnl_match:
            results["total_pnl"] = float(pnl_match.group(1).replace(",", ""))

        # Try to find trade count
        count_match = re.search(r"(\d+)\s*(?:total\s*)?trades", all_text, re.IGNORECASE)
        if count_match:
            results["total_trades"] = int(count_match.group(1))

        # Aggregate screenshot data
        img_data = self.get_screenshot_data(name)
        for img in img_data:
            nums = img.get("extracted_numbers", {})
            if "wins" in nums and "losses" in nums:
                w, l = nums["wins"], nums["losses"]
                results["wins"] = w
                results["losses"] = l
                results["win_rate"] = w / (w + l) if (w + l) > 0 else 0

        results["text_length"] = len(all_text)
        results["has_screenshots"] = len(self.get_screenshot_data(name)) > 0

        return results

    def _analyze_timing(self, timestamps: pd.Series) -> dict:
        """Analyze what hours of the day the trader is most active."""
        hours = timestamps.dt.hour
        hour_counts = hours.value_counts().sort_index()
        peak_hour = int(hour_counts.idxmax()) if not hour_counts.empty else -1

        # Day of week analysis
        dow = timestamps.dt.day_name()
        dow_counts = dow.value_counts()

        return {
            "peak_hour_utc": peak_hour,
            "trades_per_hour": hour_counts.to_dict(),
            "active_hours": int(hour_counts[hour_counts > 0].count()),
            "trades_per_day_of_week": dow_counts.to_dict(),
        }

    def compare_traders(self) -> pd.DataFrame:
        """Compare all loaded traders side by side."""
        rows = []
        for name in self.traders:
            analysis = self.analyze_trader(name)
            rows.append(analysis)

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        if "win_rate" in df.columns:
            df = df.sort_values("win_rate", ascending=False)
        return df

    def classify_strategy(self, name: str) -> str:
        """Attempt to classify what strategy a trader is using based on their
        trade patterns."""
        analysis = self.analyze_trader(name)
        if "error" in analysis:
            return "unknown"

        clues = []

        # High win rate + trades later in interval → latency arb
        win_rate = analysis.get("win_rate", 0)
        avg_entry_time = analysis.get("avg_seconds_into_interval", 150)
        if win_rate > 0.80 and avg_entry_time > 120:
            clues.append("latency_arb")

        # Buys cheap options (< $0.35) → buy-low strategy
        if analysis.get("pct_entries_below_35c", 0) > 0.5:
            clues.append("buy_low")

        # Buys expensive options (> $0.65) → confident directional bets
        if analysis.get("pct_entries_above_65c", 0) > 0.5:
            clues.append("high_confidence_directional")

        # Trades very frequently + near 50c → market making
        total = analysis.get("total_trades", 0)
        near_50 = analysis.get("pct_entries_near_50c", 0)
        if total > 500 and near_50 > 0.5:
            clues.append("market_making")

        # Low size variance → fixed position sizing
        size_cv = analysis.get("size_cv", 1)
        if size_cv < 0.1:
            clues.append("fixed_size")
        elif size_cv > 0.5:
            clues.append("variable_size (kelly-like)")

        # Balanced side preference → mispricing / arb (buys both sides)
        side_pref = analysis.get("side_preference", {})
        if side_pref:
            values = list(side_pref.values())
            if len(values) >= 2:
                ratio = min(values) / max(values) if max(values) > 0 else 0
                if ratio > 0.4:
                    clues.append("mispricing_arb")

        # Zero fees → maker-only orders
        if analysis.get("pct_zero_fee", 0) > 0.9:
            clues.append("maker_only")

        # Also check text for strategy keywords
        text = self.get_raw_text(name).lower()
        if "latency" in text or "arbitrage" in text or "arb" in text:
            clues.append("latency_arb (mentioned in docs)")
        if "market mak" in text:
            clues.append("market_making (mentioned in docs)")
        if "spread" in text and "bid" in text:
            clues.append("spread_trading (mentioned in docs)")

        if not clues:
            if win_rate > 0.6:
                return "profitable_unknown"
            return "unprofitable_or_unknown"

        return " + ".join(clues)

    def extract_strategy_params(self, name: str) -> dict:
        """Extract concrete strategy parameters from a successful trader's data
        that can be fed into our bot's strategy config."""
        analysis = self.analyze_trader(name)
        if "error" in analysis:
            return {}

        params = {
            "strategy_type": self.classify_strategy(name),
            "suggested_position_size": analysis.get("median_size", 100),
            "suggested_entry_price_max": analysis.get("median_entry_price", 0.50),
            "win_rate_benchmark": analysis.get("win_rate", 0.5),
        }

        if "avg_seconds_into_interval" in analysis:
            params["optimal_entry_window_seconds"] = (
                int(analysis["avg_seconds_into_interval"]),
                min(285, int(analysis["avg_seconds_into_interval"]) + 60),
            )

        # Profit factor drives sizing aggressiveness
        pf = analysis.get("profit_factor", 0)
        if pf > 3.0:
            params["kelly_fraction"] = 0.25  # quarter Kelly
            params["aggressive_sizing"] = True
            params["confidence_level"] = "high"
        elif pf > 2.0:
            params["kelly_fraction"] = 0.15
            params["aggressive_sizing"] = True
            params["confidence_level"] = "medium"
        elif pf > 1.5:
            params["kelly_fraction"] = 0.10
            params["aggressive_sizing"] = False
            params["confidence_level"] = "low"
        else:
            params["kelly_fraction"] = 0.05  # very conservative
            params["aggressive_sizing"] = False
            params["confidence_level"] = "avoid"

        # Entry price range
        price_dist = analysis.get("entry_price_distribution", {})
        if price_dist:
            params["preferred_price_range"] = {
                k: v for k, v in price_dist.items() if v > 0.15
            }

        return params

    def full_report(self) -> str:
        """Generate a comprehensive text report of all traders and findings."""
        lines = []
        lines.append("=" * 70)
        lines.append("  POLYMARKET TRADER ANALYSIS — FULL REPORT")
        lines.append("=" * 70)

        for name in self.traders:
            inv = self.get_inventory(name)
            analysis = self.analyze_trader(name)
            strategy = self.classify_strategy(name)
            params = self.extract_strategy_params(name)

            lines.append(f"\n{'─' * 60}")
            lines.append(f"  TRADER: {name}")
            lines.append(f"{'─' * 60}")

            lines.append(f"\n  Files: {inv}")
            lines.append(f"  Strategy: {strategy}")
            lines.append(f"  Data Source: {analysis.get('data_source', 'unknown')}")
            lines.append(f"  Total Trades: {analysis.get('total_trades', 'N/A')}")

            if "win_rate" in analysis:
                lines.append(f"  Win Rate: {analysis['win_rate']:.1%}")
            if "total_pnl" in analysis:
                lines.append(f"  Total P&L: ${analysis['total_pnl']:.2f}")
            if "profit_factor" in analysis:
                lines.append(f"  Profit Factor: {analysis['profit_factor']:.2f}")
            if "sharpe" in analysis:
                lines.append(f"  Sharpe Ratio: {analysis['sharpe']:.3f}")
            if "max_win_streak" in analysis:
                lines.append(f"  Max Win Streak: {analysis['max_win_streak']}")
                lines.append(f"  Max Loss Streak: {analysis['max_loss_streak']}")

            if "entry_price_distribution" in analysis:
                lines.append(f"  Entry Price Distribution:")
                for bucket, pct in analysis["entry_price_distribution"].items():
                    bar = "#" * int(pct * 40)
                    lines.append(f"    {bucket:>8}: {pct:5.1%} {bar}")

            if "entry_timing_distribution" in analysis:
                lines.append(f"  Entry Timing (within 5-min interval):")
                for window, pct in analysis["entry_timing_distribution"].items():
                    bar = "#" * int(pct * 40)
                    lines.append(f"    {window:>10}: {pct:5.1%} {bar}")

            # Screenshot insights
            screenshots = self.get_screenshot_data(name)
            if screenshots:
                lines.append(f"\n  Screenshots ({len(screenshots)}):")
                for img in screenshots:
                    nums = img.get("extracted_numbers", {})
                    lines.append(f"    {img['filename']}: {nums if nums else 'no data extracted'}")

            # Recommended params
            lines.append(f"\n  Recommended Bot Params:")
            for k, v in params.items():
                lines.append(f"    {k}: {v}")

        # Comparison
        lines.append(f"\n{'=' * 70}")
        lines.append("  TRADER COMPARISON (sorted by win rate)")
        lines.append(f"{'=' * 70}")

        comparison = self.compare_traders()
        if not comparison.empty:
            cols = ["name", "total_trades", "win_rate", "total_pnl", "profit_factor"]
            available = [c for c in cols if c in comparison.columns]
            if available:
                lines.append(comparison[available].to_string(index=False))

        return "\n".join(lines)
