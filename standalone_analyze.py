#!/usr/bin/env python3
"""STANDALONE Polymarket trader analyzer — single file, no project setup needed.

Handles CSVs, PDFs, screenshots (OCR), JSON, Excel, and text files.

Usage:
    pip install pandas numpy pdfplumber Pillow pytesseract tabulate
    python standalone_analyze.py ~/Downloads/polymarket-bot/
    python standalone_analyze.py ~/Downloads/polymarket-bot/ --report output.txt
    python standalone_analyze.py ~/Downloads/polymarket-bot/ --text
"""

import io
import os
import re
import sys
import argparse
import logging
from pathlib import Path

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

try:
    import pdfplumber
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

try:
    from PIL import Image
    import pytesseract
    HAS_OCR = True
except ImportError:
    HAS_OCR = False

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}


class FileInventory:
    def __init__(self, folder: Path):
        self.folder = folder
        self.csvs, self.jsons, self.excels = [], [], []
        self.pdfs, self.images, self.texts, self.unknown = [], [], [], []
        for f in sorted(folder.rglob("*")):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in {".csv", ".tsv"}: self.csvs.append(f)
            elif ext == ".json": self.jsons.append(f)
            elif ext in {".xlsx", ".xls"}: self.excels.append(f)
            elif ext == ".pdf": self.pdfs.append(f)
            elif ext in IMAGE_EXTENSIONS: self.images.append(f)
            elif ext in {".txt", ".md", ".log"}: self.texts.append(f)
            else: self.unknown.append(f)

    @property
    def total(self):
        return sum(len(x) for x in [self.csvs, self.jsons, self.excels, self.pdfs, self.images, self.texts, self.unknown])

    def summary(self):
        return {"csv": len(self.csvs), "json": len(self.jsons), "excel": len(self.excels),
                "pdf": len(self.pdfs), "images": len(self.images), "text": len(self.texts),
                "unknown": len(self.unknown), "total": self.total}


class PDFExtractor:
    @staticmethod
    def extract_tables(pdf_path):
        if not HAS_PDF: return []
        frames = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    for table in page.extract_tables():
                        if not table or len(table) < 2: continue
                        header = [str(c).strip() if c else f"col_{i}" for i, c in enumerate(table[0])]
                        df = pd.DataFrame(table[1:], columns=header).dropna(how="all").dropna(axis=1, how="all")
                        if not df.empty:
                            frames.append(df)
                            logger.info("Extracted table (%d rows) from %s", len(df), pdf_path.name)
        except Exception:
            logger.exception("Failed to extract tables from %s", pdf_path.name)
        return frames

    @staticmethod
    def extract_text(pdf_path):
        if not HAS_PDF: return ""
        parts = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text: parts.append(text)
        except Exception:
            logger.exception("Failed to extract text from %s", pdf_path.name)
        return "\n".join(parts)

    @staticmethod
    def extract_trades_from_text(text):
        patterns = {
            "full": re.compile(
                r"(\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}(?::\d{2})?)"
                r"\s+(BUY|SELL)\s+(Up|Down|YES|NO)"
                r"\s+\$?([\d.]+)\s+([\d.]+)\s+[+-]?\$?([\d.]+)", re.I),
            "simple": re.compile(
                r"(BUY|SELL)\s+(Up|Down|YES|NO)\s+\$?([\d.]+)\s+([\d.]+)", re.I),
        }
        rows = []
        for line in text.split("\n"):
            m = patterns["full"].search(line)
            if m:
                rows.append({"timestamp": m.group(1), "side": m.group(2).upper(),
                             "outcome_type": m.group(3).upper(), "entry_price": float(m.group(4)),
                             "size": float(m.group(5)), "pnl": float(m.group(6))})
                continue
            m = patterns["simple"].search(line)
            if m:
                rows.append({"side": m.group(1).upper(), "outcome_type": m.group(2).upper(),
                             "entry_price": float(m.group(3)), "size": float(m.group(4))})
        return pd.DataFrame(rows) if rows else None


class ImageExtractor:
    @staticmethod
    def extract_text(image_path):
        if not HAS_OCR: return ""
        try:
            img = Image.open(image_path)
            text = pytesseract.image_to_string(img)
            if text.strip():
                logger.info("OCR extracted %d chars from %s", len(text), image_path.name)
            return text
        except Exception:
            logger.exception("OCR failed for %s", image_path.name)
            return ""

    @staticmethod
    def extract_numbers(text):
        results = {}
        pnl = re.findall(r"[+-]?\$[\d,]+\.?\d*", text)
        if pnl: results["pnl_values"] = [float(m.replace("$","").replace(",","")) for m in pnl]
        pct = re.findall(r"(\d+\.?\d*)%", text)
        if pct: results["percentages"] = [float(m) for m in pct]
        shares = re.findall(r"(\d+)\s*(?:shares|contracts|lots)", text, re.I)
        if shares: results["share_counts"] = [int(m) for m in shares]
        prices = re.findall(r"\$?(0\.\d+)", text)
        if prices: results["prices"] = [float(m) for m in prices]
        wl = re.search(r"(\d+)\s*(?:wins?|W)\s*[/,]\s*(\d+)\s*(?:loss|losses|L)", text, re.I)
        if wl: results["wins"], results["losses"] = int(wl.group(1)), int(wl.group(2))
        return results

    @staticmethod
    def get_image_info(image_path):
        try:
            img = Image.open(image_path)
            return {"filename": image_path.name, "size": img.size, "format": img.format, "path": str(image_path)}
        except Exception:
            return {"filename": image_path.name, "path": str(image_path)}


class TraderAnalyzer:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.traders = {}
        self.raw_texts = {}
        self.image_data = {}
        self.inventories = {}

    def load_all(self):
        if not self.data_dir.exists():
            logger.warning("Data directory %s does not exist", self.data_dir)
            return {}

        folders = [item for item in sorted(self.data_dir.iterdir()) if item.is_dir()]
        if not folders:
            folders = [self.data_dir]

        for folder in folders:
            name = folder.name
            inv = FileInventory(folder)
            self.inventories[name] = inv
            logger.info("Trader '%s': %s", name, inv.summary())

            df = self._load_structured(inv)
            pdf_df, pdf_texts = self._load_pdfs(inv)
            if pdf_texts: self.raw_texts.setdefault(name, []).extend(pdf_texts)

            img_data, img_texts = self._load_images(inv)
            if img_data: self.image_data[name] = img_data
            if img_texts: self.raw_texts.setdefault(name, []).extend(img_texts)

            txt_texts = self._load_texts(inv)
            if txt_texts: self.raw_texts.setdefault(name, []).extend(txt_texts)

            frames = []
            if df is not None and not df.empty: frames.append(df)
            if pdf_df is not None and not pdf_df.empty: frames.append(pdf_df)

            if frames:
                combined = self._normalize(pd.concat(frames, ignore_index=True))
                self.traders[name] = combined
                logger.info("Loaded %d trades for '%s'", len(combined), name)
            elif self.raw_texts.get(name) or self.image_data.get(name):
                self.traders[name] = pd.DataFrame()
                logger.info("'%s': text/images only (%d texts, %d images)", name,
                            len(self.raw_texts.get(name, [])), len(self.image_data.get(name, [])))

        logger.info("Loaded %d traders total", len(self.traders))
        return self.traders

    def _load_structured(self, inv):
        frames = []
        for f in inv.csvs:
            try:
                for enc in ["utf-8", "latin-1", "cp1252"]:
                    try: df = pd.read_csv(f, encoding=enc); break
                    except UnicodeDecodeError: continue
                else: df = pd.read_csv(f, encoding="utf-8", errors="replace")
                if not df.empty:
                    df["_source"] = f.name; frames.append(df)
                    logger.info("CSV %s: %d rows", f.name, len(df))
            except Exception: logger.warning("Failed CSV %s", f.name)

        for f in inv.jsons:
            try:
                df = pd.read_json(f)
                if not df.empty:
                    df["_source"] = f.name; frames.append(df)
                    logger.info("JSON %s: %d rows", f.name, len(df))
            except Exception: logger.warning("Failed JSON %s", f.name)

        for f in inv.excels:
            try:
                df = pd.read_excel(f)
                if not df.empty:
                    df["_source"] = f.name; frames.append(df)
                    logger.info("Excel %s: %d rows", f.name, len(df))
            except Exception: logger.warning("Failed Excel %s", f.name)

        return pd.concat(frames, ignore_index=True) if frames else None

    def _load_pdfs(self, inv):
        frames, texts = [], []
        for f in inv.pdfs:
            for tbl in PDFExtractor.extract_tables(f):
                tbl["_source"] = f.name; frames.append(tbl)
            text = PDFExtractor.extract_text(f)
            if text.strip():
                texts.append(f"[PDF: {f.name}]\n{text}")
                tdf = PDFExtractor.extract_trades_from_text(text)
                if tdf is not None and not tdf.empty:
                    tdf["_source"] = f"{f.name} (parsed)"; frames.append(tdf)
        return (pd.concat(frames, ignore_index=True) if frames else None), texts

    def _load_images(self, inv):
        data, texts = [], []
        for f in inv.images:
            info = ImageExtractor.get_image_info(f)
            text = ImageExtractor.extract_text(f)
            if text.strip():
                texts.append(f"[Screenshot: {f.name}]\n{text}")
                info["ocr_text"] = text[:500]
                info["extracted_numbers"] = ImageExtractor.extract_numbers(text)
            else:
                info["ocr_text"] = ""; info["extracted_numbers"] = {}
            data.append(info)
        return data, texts

    def _load_texts(self, inv):
        texts = []
        for f in inv.texts:
            try:
                text = f.read_text(errors="replace")
                if text.strip(): texts.append(f"[Text: {f.name}]\n{text}")
            except Exception: pass
        return texts

    def _normalize(self, df):
        col_map = {}
        for col in df.columns:
            if col.startswith("_"): continue
            lower = col.lower().strip().replace("_", " ").replace("-", " ")
            if any(k in lower for k in ["time", "date", "timestamp", "created", "executed"]):
                col_map[col] = "timestamp"
            elif ("price" in lower and "entry" in lower) or lower in ["buy price", "fill price", "exec price"]:
                col_map[col] = "entry_price"
            elif "price" in lower and "exit" in lower: col_map[col] = "exit_price"
            elif lower in ["price", "avg price", "fill", "cost"]: col_map[col] = "entry_price"
            elif any(k in lower for k in ["side", "direction", "type", "action"]): col_map[col] = "side"
            elif any(k in lower for k in ["size", "amount", "quantity", "qty", "shares", "contracts", "volume"]):
                col_map[col] = "size"
            elif any(k in lower for k in ["pnl", "profit", "p&l", "gain", "return", "net"]): col_map[col] = "pnl"
            elif any(k in lower for k in ["outcome", "result", "won", "status", "resolved"]): col_map[col] = "outcome"
            elif any(k in lower for k in ["market", "slug", "event", "question"]): col_map[col] = "market"
            elif any(k in lower for k in ["token", "asset"]): col_map[col] = "token_id"
            elif any(k in lower for k in ["fee", "commission"]): col_map[col] = "fee"

        df = df.rename(columns=col_map)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            if df["timestamp"].notna().any():
                df = df.sort_values("timestamp").reset_index(drop=True)
        for col in ["entry_price", "exit_price", "size", "pnl", "fee"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce")
        return df

    def analyze_trader(self, name):
        if name not in self.traders: return {"error": f"'{name}' not found"}
        df = self.traders[name]
        a = {"name": name, "total_trades": len(df), "files": self.inventories.get(name, FileInventory(Path("."))).summary()}

        if df.empty:
            a["data_source"] = "text/screenshots only"
            a.update(self._analyze_from_text(name))
            return a

        a["data_source"] = "tabular"

        if "outcome" in df.columns:
            wins = df["outcome"].astype(str).str.lower().isin(["win","won","true","1","yes","resolved","success"])
            a["wins"], a["losses"], a["win_rate"] = int(wins.sum()), int((~wins).sum()), float(wins.mean())
        elif "pnl" in df.columns and df["pnl"].notna().any():
            pnl = df["pnl"].dropna()
            a["wins"], a["losses"] = int((pnl>0).sum()), int((pnl<=0).sum())
            a["win_rate"] = float((pnl>0).mean())

        if "pnl" in df.columns and df["pnl"].notna().any():
            pnl = df["pnl"].dropna().astype(float)
            a["total_pnl"] = float(pnl.sum())
            a["avg_win"] = float(pnl[pnl>0].mean()) if (pnl>0).any() else 0
            a["avg_loss"] = float(pnl[pnl<=0].mean()) if (pnl<=0).any() else 0
            a["max_win"], a["max_loss"] = float(pnl.max()), float(pnl.min())
            a["profit_factor"] = float(pnl[pnl>0].sum()/abs(pnl[pnl<0].sum())) if (pnl<0).any() and pnl[pnl<0].sum()!=0 else float("inf")
            a["sharpe"] = float(pnl.mean()/pnl.std()) if pnl.std()>0 else 0
            signs = (pnl>0).astype(int)
            streaks = signs.groupby((signs!=signs.shift()).cumsum())
            ws = [len(g) for _,g in streaks if g.iloc[0]==1]
            ls = [len(g) for _,g in streaks if g.iloc[0]==0]
            a["max_win_streak"] = max(ws) if ws else 0
            a["max_loss_streak"] = max(ls) if ls else 0

        if "size" in df.columns and df["size"].notna().any():
            s = df["size"].dropna().astype(float)
            a.update({"avg_size": float(s.mean()), "median_size": float(s.median()),
                       "max_size": float(s.max()), "min_size": float(s.min()),
                       "size_stddev": float(s.std()), "size_cv": float(s.std()/s.mean()) if s.mean()>0 else 0})

        if "entry_price" in df.columns and df["entry_price"].notna().any():
            p = df["entry_price"].dropna().astype(float)
            a.update({"avg_entry_price": float(p.mean()), "median_entry_price": float(p.median()),
                       "pct_entries_below_35c": float((p<0.35).mean()),
                       "pct_entries_near_50c": float(((p>0.40)&(p<0.60)).mean()),
                       "pct_entries_above_65c": float((p>0.65).mean()),
                       "entry_price_distribution": {
                           "0-20c": float((p<0.20).mean()), "20-35c": float(((p>=0.20)&(p<0.35)).mean()),
                           "35-50c": float(((p>=0.35)&(p<0.50)).mean()), "50-65c": float(((p>=0.50)&(p<0.65)).mean()),
                           "65-80c": float(((p>=0.65)&(p<0.80)).mean()), "80c+": float((p>=0.80).mean()),
                       }})

        if "fee" in df.columns and df["fee"].notna().any():
            fees = df["fee"].dropna().astype(float)
            a.update({"total_fees": float(fees.sum()), "avg_fee": float(fees.mean()),
                       "pct_zero_fee": float((fees==0).mean())})

        if "timestamp" in df.columns and df["timestamp"].notna().any():
            ts = df["timestamp"].dropna()
            hours = ts.dt.hour; hc = hours.value_counts().sort_index()
            dow = ts.dt.day_name(); dc = dow.value_counts()
            a["trading_hours"] = {"peak_hour_utc": int(hc.idxmax()) if not hc.empty else -1,
                                   "trades_per_hour": hc.to_dict(), "active_hours": int(hc[hc>0].count()),
                                   "trades_per_day_of_week": dc.to_dict()}
            a["date_range"] = {"first_trade": str(ts.min()), "last_trade": str(ts.max()),
                                "trading_days": int(ts.dt.date.nunique())}
            sec = ts.dt.second + (ts.dt.minute%5)*60
            a["avg_seconds_into_interval"] = float(sec.mean())
            a["entry_timing_distribution"] = {
                "first_60s": float((sec<60).mean()),
                "60_120s": float(((sec>=60)&(sec<120)).mean()),
                "120_180s": float(((sec>=120)&(sec<180)).mean()),
                "180_240s": float(((sec>=180)&(sec<240)).mean()),
                "last_60s": float((sec>=240).mean()),
            }

        if "side" in df.columns and df["side"].notna().any():
            a["side_preference"] = df["side"].astype(str).str.lower().value_counts().to_dict()
        if "market" in df.columns and df["market"].notna().any():
            a["unique_markets"] = int(df["market"].nunique())

        return a

    def _analyze_from_text(self, name):
        r = {}
        text = "\n\n".join(self.raw_texts.get(name, []))
        if not text: return r

        m = re.search(r"win\s*rate[:\s]*(\d+\.?\d*)%", text, re.I)
        if m: r["win_rate"] = float(m.group(1))/100
        m = re.search(r"(?:total|net)\s*(?:p[&/]?l|profit)[:\s]*[+-]?\$?([\d,]+\.?\d*)", text, re.I)
        if m: r["total_pnl"] = float(m.group(1).replace(",",""))
        m = re.search(r"(\d+)\s*(?:total\s*)?trades", text, re.I)
        if m: r["total_trades"] = int(m.group(1))

        for img in self.image_data.get(name, []):
            nums = img.get("extracted_numbers", {})
            if "wins" in nums and "losses" in nums:
                w, l = nums["wins"], nums["losses"]
                r.update({"wins": w, "losses": l, "win_rate": w/(w+l) if (w+l)>0 else 0})

        r["text_length"] = len(text)
        r["has_screenshots"] = len(self.image_data.get(name, [])) > 0
        return r

    def classify_strategy(self, name):
        a = self.analyze_trader(name)
        if "error" in a: return "unknown"
        clues = []
        wr = a.get("win_rate", 0)
        avg_t = a.get("avg_seconds_into_interval", 150)
        if wr > 0.80 and avg_t > 120: clues.append("latency_arb")
        if a.get("pct_entries_below_35c", 0) > 0.5: clues.append("buy_low")
        if a.get("pct_entries_above_65c", 0) > 0.5: clues.append("high_confidence_directional")
        total = a.get("total_trades", 0)
        if total > 500 and a.get("pct_entries_near_50c", 0) > 0.5: clues.append("market_making")
        cv = a.get("size_cv", 1)
        if cv < 0.1: clues.append("fixed_size")
        elif cv > 0.5: clues.append("variable_size")
        sp = a.get("side_preference", {})
        if sp:
            v = list(sp.values())
            if len(v) >= 2 and min(v)/max(v) > 0.4: clues.append("mispricing_arb")
        if a.get("pct_zero_fee", 0) > 0.9: clues.append("maker_only")

        text = "\n".join(self.raw_texts.get(name, [])).lower()
        if "latency" in text or "arbitrage" in text: clues.append("latency_arb (in docs)")
        if "market mak" in text: clues.append("market_making (in docs)")
        if "spread" in text and "bid" in text: clues.append("spread_trading (in docs)")

        if not clues: return "profitable_unknown" if wr > 0.6 else "unprofitable_or_unknown"
        return " + ".join(clues)

    def extract_params(self, name):
        a = self.analyze_trader(name)
        if "error" in a: return {}
        p = {"strategy": self.classify_strategy(name),
             "position_size": a.get("median_size", 100),
             "entry_price_max": a.get("median_entry_price", 0.50),
             "win_rate": a.get("win_rate", 0.5)}
        if "avg_seconds_into_interval" in a:
            p["entry_window"] = (int(a["avg_seconds_into_interval"]), min(285, int(a["avg_seconds_into_interval"])+60))
        pf = a.get("profit_factor", 0)
        if pf > 3: p.update(kelly=0.25, confidence="HIGH")
        elif pf > 2: p.update(kelly=0.15, confidence="MEDIUM")
        elif pf > 1.5: p.update(kelly=0.10, confidence="LOW")
        else: p.update(kelly=0.05, confidence="AVOID")
        return p

    def compare(self):
        rows = [self.analyze_trader(n) for n in self.traders]
        if not rows: return pd.DataFrame()
        df = pd.DataFrame(rows)
        return df.sort_values("win_rate", ascending=False) if "win_rate" in df.columns else df


def main():
    parser = argparse.ArgumentParser(description="Analyze Polymarket trader data")
    parser.add_argument("data_dir", help="Folder containing trader subfolders")
    parser.add_argument("--report", "-r", help="Save report to file")
    parser.add_argument("--text", action="store_true", help="Print extracted text")
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    if not HAS_PDF: print("NOTE: pdfplumber not installed — PDF parsing disabled. Run: pip install pdfplumber")
    if not HAS_OCR: print("NOTE: pytesseract/Pillow not installed — screenshot OCR disabled. Run: pip install Pillow pytesseract")

    az = TraderAnalyzer(args.data_dir)
    traders = az.load_all()

    if not traders:
        print(f"\nNo data found in {args.data_dir}")
        print("Expected: subfolders with CSVs, PDFs, screenshots, etc.")
        sys.exit(1)

    # File inventory
    print("\n" + "=" * 70)
    print("  FILE INVENTORY")
    print("=" * 70)
    for name in traders:
        inv = az.inventories[name].summary()
        print(f"\n  {name}:")
        for t, c in inv.items():
            if c > 0 and t != "total": print(f"    {t:>8}: {c} files")
        print(f"    {'total':>8}: {inv['total']} files")

    # Per-trader analysis
    for name in traders:
        a = az.analyze_trader(name)
        strat = az.classify_strategy(name)
        params = az.extract_params(name)

        print(f"\n{'─' * 60}")
        print(f"  TRADER: {name}")
        print(f"{'─' * 60}")
        print(f"  Strategy: {strat}")
        print(f"  Data: {a.get('data_source', '?')} | Trades: {a.get('total_trades', 'N/A')}")
        if "win_rate" in a: print(f"  Win Rate: {a['win_rate']:.1%}")
        if "total_pnl" in a: print(f"  Total P&L: ${a['total_pnl']:.2f}")
        if "profit_factor" in a: print(f"  Profit Factor: {a['profit_factor']:.2f}")
        if "sharpe" in a: print(f"  Sharpe: {a['sharpe']:.3f}")
        if "max_win_streak" in a: print(f"  Streaks: {a['max_win_streak']}W / {a['max_loss_streak']}L")
        if "avg_size" in a: print(f"  Size: avg={a['avg_size']:.1f} med={a.get('median_size',0):.1f} max={a.get('max_size',0):.1f}")
        if "avg_entry_price" in a: print(f"  Entry Price: avg=${a['avg_entry_price']:.3f} | <35c: {a.get('pct_entries_below_35c',0):.0%} | ~50c: {a.get('pct_entries_near_50c',0):.0%} | >65c: {a.get('pct_entries_above_65c',0):.0%}")

        if "entry_price_distribution" in a:
            print("  Price Distribution:")
            for b, pct in a["entry_price_distribution"].items():
                print(f"    {b:>8}: {pct:5.1%} {'#'*int(pct*40)}")

        if "entry_timing_distribution" in a:
            print("  Entry Timing (in 5-min interval):")
            for w, pct in a["entry_timing_distribution"].items():
                print(f"    {w:>10}: {pct:5.1%} {'#'*int(pct*40)}")

        imgs = az.image_data.get(name, [])
        if imgs:
            print(f"  Screenshots ({len(imgs)}):")
            for img in imgs:
                nums = img.get("extracted_numbers", {})
                print(f"    {img['filename']}: {nums if nums else 'no data extracted'}")

        print(f"  Bot Params: {params}")

    # Comparison
    print(f"\n{'=' * 70}")
    print("  COMPARISON (sorted by win rate)")
    print(f"{'=' * 70}")
    comp = az.compare()
    if not comp.empty:
        cols = [c for c in ["name","total_trades","win_rate","total_pnl","profit_factor"] if c in comp.columns]
        if cols: print(comp[cols].to_string(index=False))

    # Recommendation
    print(f"\n{'=' * 70}")
    print("  RECOMMENDED BOT CONFIG")
    print(f"{'=' * 70}")
    best = sorted([n for n in traders if az.analyze_trader(n).get("win_rate",0)>0.5],
                  key=lambda n: az.analyze_trader(n).get("win_rate",0), reverse=True)
    if best:
        p = az.extract_params(best[0])
        print(f"\n  Based on '{best[0]}':")
        print(f"    STRATEGY={p.get('strategy','latency_arb')}")
        print(f"    MAX_POSITION_SIZE={p.get('position_size',100)}")
        print(f"    MIN_EDGE_THRESHOLD=0.05")
        print(f"    MAKER_ONLY=true")
        print(f"    Confidence: {p.get('confidence','?')} | Kelly: {p.get('kelly',0.10)}")
    else:
        print("  No profitable traders found — using defaults")

    if args.text:
        print(f"\n{'=' * 70}")
        print("  EXTRACTED TEXT (PDFs, OCR, Notes)")
        print(f"{'=' * 70}")
        for name in traders:
            texts = az.raw_texts.get(name, [])
            if texts:
                print(f"\n{'─' * 40} {name} {'─' * 40}")
                full = "\n\n".join(texts)
                print(full[:3000])
                if len(full) > 3000: print(f"  ... ({len(full)-3000} more chars)")

    if args.report:
        # Save everything to file
        import io as _io
        old_stdout = sys.stdout
        sys.stdout = buf = _io.StringIO()
        # Re-run the output (simplified)
        for name in traders:
            a = az.analyze_trader(name)
            print(f"TRADER: {name} | {az.classify_strategy(name)} | WR:{a.get('win_rate',0):.1%} | PnL:${a.get('total_pnl',0):.2f}")
        sys.stdout = old_stdout
        with open(args.report, "w") as f:
            f.write(buf.getvalue())
        print(f"\nReport saved to {args.report}")


if __name__ == "__main__":
    main()
