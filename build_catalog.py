import os
import io
import json
import math
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

import requests
from PIL import Image as PILImage

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.colors import Color

# ✅ threading (ONLY addition)
from concurrent.futures import ThreadPoolExecutor, as_completed


# ----------------------------
# Theme
# ----------------------------
brandMaroon = colors.HexColor("#7A1631")
brandMaroonDark = colors.HexColor("#5F1026")
accentGreen = colors.HexColor("#1E8E3E")
ink = colors.HexColor("#111111")
muted = colors.HexColor("#666666")
ruleGrey = colors.HexColor("#D8D8D8")
cardFill = colors.HexColor("#FAFAFA")


# ----------------------------
# Paths / Config
# ----------------------------
baseDir = os.path.dirname(os.path.abspath(__file__))
jsonPath = os.path.join(baseDir, "products.json")
outputPdfPath = os.path.join(baseDir, "catalog.pdf")

imageUrlTemplate = "https://superasia.ca/web/image/product.product/{productId}/image_512"
imageCacheDir = os.path.join(baseDir, "_image_cache")

maxProductsTotal: Optional[int] = None

httpTimeoutSeconds = 20
httpRetries = 3
httpHeaders = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome Safari"
    )
}

cachedJpegQuality = 72
cachedMaxPixels = 512
cachedMinBytes = 2000
jpegOptimize = True
jpegProgressive = True

tocShowCategoriesIfMoreThan = 5


# ----------------------------
# Page / Layout
# ----------------------------
pageWidth, pageHeight = LETTER

marginLeft = 0.55 * inch
marginRight = 0.55 * inch
marginTop = 0.55 * inch
marginBottom = 0.60 * inch

footerReserved = 0.50 * inch

headerBarHeight = 0.42 * inch
headerGapBelow = 0.18 * inch

columns = 2
rowsPerPage = 3
itemsPerPage = columns * rowsPerPage

gutter = 0.35 * inch
contentWidth = pageWidth - marginLeft - marginRight
columnWidth = (contentWidth - gutter) / columns

rowGap = 12

cardPadding = 7


# ----------------------------
# Styles
# ----------------------------
titleStyle = ParagraphStyle("TitleStyle", fontName="Helvetica-Bold", fontSize=15, leading=17, textColor=ink)
sectionStyle = ParagraphStyle("SectionStyle", fontName="Helvetica-Bold", fontSize=11, leading=13, textColor=ink)

nameStyle = ParagraphStyle("NameStyle", fontName="Helvetica-Bold", fontSize=10, leading=12, alignment=1, textColor=ink)
priceStyle = ParagraphStyle("PriceStyle", fontName="Helvetica", fontSize=10, leading=12, alignment=1, textColor=ink)
offerBadgeStyle = ParagraphStyle(
    "OfferBadgeStyle", fontName="Helvetica-Bold", fontSize=9, leading=11, alignment=1, textColor=colors.white
)

smallGreyStyle = ParagraphStyle("SmallGreyStyle", fontName="Helvetica", fontSize=9, leading=11, textColor=muted)
tocTitleStyle = ParagraphStyle("TocTitleStyle", fontName="Helvetica-Bold", fontSize=14, leading=16, textColor=ink)


# ----------------------------
# Helpers
# ----------------------------
def ensureDir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def safeStr(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def parseFloatSafe(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except Exception:
        return None


def money(value: Any) -> Optional[float]:
    v = parseFloatSafe(value)
    if v is None:
        return None
    return round(v + 1e-9, 2)


def rangeText(startPage: int, endPage: int) -> str:
    return f"(Page {startPage})" if startPage == endPage else f"(Pages {startPage}–{endPage})"


def rangePlain(startPage: int, endPage: int) -> str:
    return f"{startPage}" if startPage == endPage else f"{startPage}–{endPage}"


def wrapTextLines(text: str, fontName: str, fontSize: int, maxWidth: float, maxLines: int) -> List[str]:
    text = safeStr(text)
    if not text:
        return [""]

    def fits(s: str) -> bool:
        return stringWidth(s, fontName, fontSize) <= maxWidth

    words = text.split()
    lines: List[str] = []
    current = ""

    for w in words:
        candidate = w if not current else f"{current} {w}"
        if fits(candidate):
            current = candidate
            continue

        if current:
            lines.append(current)
        current = w

        if len(lines) >= maxLines:
            break

    if len(lines) < maxLines and current:
        lines.append(current)

    usedWords = sum(len(line.split()) for line in lines)
    if usedWords < len(words):
        last = lines[-1]
        ell = "…"
        while last and not fits(last + ell):
            last = last[:-1].rstrip()
        lines[-1] = (last + ell) if last else ell

    return lines[:maxLines]


def measureParagraphHeight(text: str, style: ParagraphStyle, width: float) -> float:
    p = Paragraph(text, style)
    _, h = p.wrap(width, 10000)
    return h


def drawParagraph(c: canvas.Canvas, text: str, style: ParagraphStyle, x: float, yTop: float, width: float) -> float:
    p = Paragraph(text, style)
    _, h = p.wrap(width, 10000)
    p.drawOn(c, x, yTop - h)
    return h


# ----------------------------
# Data
# ----------------------------
def loadProducts(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, dict):
        return list(raw.values())
    if isinstance(raw, list):
        return raw

    raise ValueError("Unexpected JSON structure (expected dict or list)")


def getSecondCategory(categoryStr: str) -> str:
    if not categoryStr:
        return "Other"
    parts = [p.strip() for p in categoryStr.split("/") if p.strip()]
    return parts[1] if len(parts) >= 2 else parts[0]


def getBrand(product: Dict[str, Any]) -> str:
    for key in ("brand", "brandName", "manufacturer"):
        b = safeStr(product.get(key))
        if b:
            return b
    return "Other"


def minPriceListPrice(product: Dict[str, Any]) -> Optional[float]:
    lowest = None
    for tier in product.get("priceList", []) or []:
        fp = money(tier.get("fixedPrice"))
        if fp is None:
            continue
        lowest = fp if lowest is None else min(lowest, fp)
    return lowest


def isOfferProduct(product: Dict[str, Any]) -> bool:
    reg = money(product.get("regularPrice"))
    if reg is None:
        reg = money(product.get("price"))
    if reg is None:
        return False

    lowest = minPriceListPrice(product)
    return (lowest is not None) and (lowest < reg)


def getOfferPrice(product: Dict[str, Any]) -> Optional[float]:
    return minPriceListPrice(product) if isOfferProduct(product) else None


def buildHierarchy(products: List[Dict[str, Any]], offerOnly: bool) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    filtered = [p for p in products if (isOfferProduct(p) if offerOnly else (not isOfferProduct(p)))]

    filtered.sort(
        key=lambda p: (
            getBrand(p).upper(),
            getSecondCategory(safeStr(p.get("category"))).upper(),
            safeStr(p.get("name")).upper(),
        )
    )

    hierarchy: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for p in filtered:
        brand = getBrand(p)
        category = getSecondCategory(safeStr(p.get("category")))
        hierarchy.setdefault(brand, {}).setdefault(category, []).append(p)

    return hierarchy


# ----------------------------
# Images
# ----------------------------
def getCachedImagePath(productId: int) -> str:
    ensureDir(imageCacheDir)
    return os.path.join(imageCacheDir, f"{productId}.jpg")


def normalizeAndCompressImage(img: PILImage.Image) -> PILImage.Image:
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")

    if img.mode == "RGBA":
        bg = PILImage.new("RGBA", img.size, (255, 255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg.convert("RGB")
    else:
        img = img.convert("RGB")

    w, h = img.size
    maxSide = max(w, h)
    if maxSide > cachedMaxPixels:
        scale = cachedMaxPixels / float(maxSide)
        newW = max(1, int(w * scale))
        newH = max(1, int(h * scale))
        img = img.resize((newW, newH), PILImage.LANCZOS)

    return img


def downloadImageToCache(productId: int) -> Optional[str]:
    localPath = getCachedImagePath(productId)

    if os.path.exists(localPath) and os.path.getsize(localPath) > cachedMinBytes:
        return localPath

    url = imageUrlTemplate.format(productId=productId)

    for _ in range(httpRetries):
        try:
            r = requests.get(url, timeout=httpTimeoutSeconds, headers=httpHeaders)
            if r.status_code != 200 or not r.content:
                continue

            img = PILImage.open(io.BytesIO(r.content))
            img = normalizeAndCompressImage(img)
            img.save(
                localPath,
                "JPEG",
                quality=cachedJpegQuality,
                optimize=jpegOptimize,
                progressive=jpegProgressive,
            )
            return localPath
        except Exception:
            continue

    return None


def computeFitSizePreferWidth(localImagePath: str, targetWidth: float, maxHeight: float) -> Tuple[float, float]:
    img = PILImage.open(localImagePath)
    w, h = img.size
    if w <= 0 or h <= 0:
        return targetWidth, maxHeight

    scale = targetWidth / float(w)
    newW = targetWidth
    newH = float(h) * scale

    if newH > maxHeight:
        scale2 = maxHeight / newH
        newH = maxHeight
        newW = newW * scale2

    return newW, newH


# ✅ NEW: threaded prefetch (ONLY addition)
def prefetchImages(products: List[Dict[str, Any]], maxWorkers: int = 12) -> None:
    productIds: List[int] = []
    for p in products:
        try:
            pid = int(p.get("id") or 0)
            if pid > 0:
                productIds.append(pid)
        except Exception:
            continue

    if not productIds:
        return

    ensureDir(imageCacheDir)
    print(f"📥 Prefetching {len(productIds)} images with {maxWorkers} threads...")

    with ThreadPoolExecutor(max_workers=maxWorkers) as executor:
        futures = [executor.submit(downloadImageToCache, pid) for pid in productIds]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass

    print("✅ Prefetch complete")


# ----------------------------
# Pricing text
# ----------------------------
def buildCompactPriceLines(product: Dict[str, Any], isOffer: bool) -> List[str]:
    if isOffer:
        reg = money(product.get("regularPrice"))
        if reg is None:
            reg = money(product.get("price"))
        offer = getOfferPrice(product)

        lines: List[str] = []
        if offer is not None:
            lines.append(f"<b><font color='{accentGreen.hexval()}'>Now: ${offer:,.2f}</font></b>")
        if reg is not None:
            lines.append(f"<font color='{muted.hexval()}'>Regular: ${reg:,.2f}</font>")
        return lines[:2]

    base = money(product.get("price"))
    return [f"Price: ${base:,.2f}"] if base is not None else ["Price: N/A"]


# ----------------------------
# Header / Footer
# ----------------------------
def drawFooter(c: canvas.Canvas, pageNo: int, generatedOn: str) -> None:
    yLine = marginBottom + 6
    c.setStrokeColor(ruleGrey)
    c.setLineWidth(0.6)
    c.line(marginLeft, yLine, marginLeft + contentWidth, yLine)

    y = marginBottom - 14
    c.setFillColor(muted)
    c.setFont("Helvetica", 9)
    c.drawString(marginLeft, y, f"Generated {generatedOn}")

    rightText = f"Page {pageNo}"
    w = stringWidth(rightText, "Helvetica", 9)
    c.drawString(marginLeft + contentWidth - w, y, rightText)
    c.setFillColor(ink)


def drawBrandCategoryBar(c: canvas.Canvas, brandName: str, brandRange: str, categoryName: str, yStartTop: float) -> float:
    barY = yStartTop - headerBarHeight

    c.setFillColor(brandMaroon)
    c.rect(marginLeft, barY, contentWidth, headerBarHeight, fill=1, stroke=0)

    padX = 12
    midY = barY + headerBarHeight / 2.0

    leftText = f"{brandName.upper()} {brandRange}".strip()
    rightText = safeStr(categoryName).title()

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(marginLeft + padX, midY - 5, leftText)

    c.setFont("Helvetica", 11)
    sep = " | "
    sepW = stringWidth(sep, "Helvetica", 11)
    catW = stringWidth(rightText, "Helvetica", 11)
    totalW = sepW + catW
    rightX = marginLeft + contentWidth - padX - totalW

    c.setFillColor(colors.HexColor("#F2F2F2"))
    c.drawString(rightX, midY - 5, sep)

    c.setFillColor(colors.white)
    c.drawString(rightX + sepW, midY - 5, rightText)

    yAfter = barY - 8
    c.setStrokeColor(ruleGrey)
    c.setLineWidth(0.8)
    c.line(marginLeft, yAfter, marginLeft + contentWidth, yAfter)

    return yAfter - (headerGapBelow - 8)


# ----------------------------
# TOC rendering
# ----------------------------
def drawTocRow(c: canvas.Canvas, leftText: str, rightText: str, x: float, y: float, width: float, indent: float = 0) -> float:
    fontName = "Helvetica"
    fontSize = 10

    leftText = safeStr(leftText)
    rightText = safeStr(rightText)

    c.setFont(fontName, fontSize)
    c.setFillColor(ink)

    leftX = x + indent
    c.drawString(leftX, y, leftText)

    rightW = stringWidth(rightText, fontName, fontSize) if rightText else 0
    rightX = x + width - rightW
    if rightText:
        c.drawString(rightX, y, rightText)

    dotStart = leftX + stringWidth(leftText, fontName, fontSize) + 6
    dotEnd = (rightX - 6) if rightText else (x + width - 10)
    if dotEnd > dotStart:
        dotCharW = stringWidth(".", fontName, fontSize)
        count = int((dotEnd - dotStart) / max(dotCharW, 0.1))
        c.setFillColor(muted)
        c.drawString(dotStart, y, "." * max(0, min(count, 300)))
        c.setFillColor(ink)

    return 16


# ----------------------------
# Product card
# ----------------------------
def drawFloatingUomBadge(c: canvas.Canvas, xRight: float, yTop: float, text: str) -> None:
    text = safeStr(text)
    if not text:
        return

    fontName = "Helvetica-Bold"
    fontSize = 9
    padX = 7
    padY = 3

    textW = stringWidth(text, fontName, fontSize)
    badgeW = textW + padX * 2
    badgeH = fontSize + padY * 2

    x = xRight - badgeW
    y = yTop - badgeH

    c.saveState()
    try:
        c.setFillAlpha(0.55)
        c.setStrokeAlpha(0.35)
        c.setFillColor(Color(1, 1, 1, alpha=0.55))
        c.setStrokeColor(Color(0.37, 0.06, 0.15, alpha=0.35))
    except Exception:
        c.setFillColor(colors.HexColor("#F6F6F6"))
        c.setStrokeColor(colors.HexColor("#C9A3AE"))

    c.setLineWidth(0.8)
    c.roundRect(x, y, badgeW, badgeH, badgeH / 2, fill=1, stroke=1)

    try:
        c.setFillAlpha(1)
    except Exception:
        pass

    c.setFillColor(brandMaroonDark)
    c.setFont(fontName, fontSize)
    c.drawString(x + padX, y + padY + 1, text)

    c.restoreState()


def drawProductCardFixed(
    c: canvas.Canvas,
    x: float,
    yTop: float,
    cardWidth: float,
    cardHeight: float,
    product: Dict[str, Any],
    isOffer: bool,
) -> None:
    c.setStrokeColor(ruleGrey)
    c.setFillColor(cardFill)
    c.setLineWidth(0.8)
    c.roundRect(x, yTop - cardHeight, cardWidth, cardHeight, 10, stroke=1, fill=1)

    innerX = x + cardPadding
    innerW = cardWidth - (cardPadding * 2)
    yBottom = yTop - cardHeight

    name = safeStr(product.get("name"))
    nameLines = wrapTextLines(name, "Helvetica-Bold", 10, innerW, maxLines=2)
    safeName = "<br/>".join([l.replace("&", "&amp;") for l in nameLines]) or "&nbsp;"

    priceLines = buildCompactPriceLines(product, isOffer=isOffer)
    priceText = "<br/>".join(priceLines) if priceLines else "Price: N/A"

    nameH = measureParagraphHeight(safeName, nameStyle, innerW)
    priceH = measureParagraphHeight(priceText, priceStyle, innerW)

    textTopGap = 6
    textBetweenGap = 4
    textBottomGap = 10

    textZoneH = textTopGap + nameH + textBetweenGap + priceH + textBottomGap
    if isOffer:
        textZoneH += 14

    imageTextGap = 6
    maxImageZoneH = cardHeight - (cardPadding * 2) - textZoneH - imageTextGap
    maxImageZoneH = max(0.9 * inch, maxImageZoneH)

    imageZoneTop = yTop - cardPadding
    imageZoneBottom = imageZoneTop - maxImageZoneH
    imageZoneH = maxImageZoneH

    c.setFillColor(colors.white)
    c.rect(innerX, imageZoneBottom, innerW, imageZoneH, fill=1, stroke=0)

    productId = int(product.get("id") or 0)
    localPath = downloadImageToCache(productId) if productId else None

    if localPath and os.path.exists(localPath):
        imgW, imgH = computeFitSizePreferWidth(localPath, innerW, imageZoneH)
        imgX = innerX + (innerW - imgW) / 2
        imgY = imageZoneBottom + (imageZoneH - imgH) / 2
        c.drawImage(localPath, imgX, imgY, width=imgW, height=imgH, preserveAspectRatio=True)
    else:
        c.setFillColor(colors.whitesmoke)
        c.setStrokeColor(ruleGrey)
        c.rect(innerX, imageZoneBottom, innerW, imageZoneH, fill=1, stroke=1)
        c.setFillColor(muted)
        c.setFont("Helvetica", 8)
        c.drawCentredString(innerX + innerW / 2, imageZoneBottom + imageZoneH / 2, "No Image")

    uom = safeStr(product.get("uom"))
    if uom:
        badgePad = 6
        drawFloatingUomBadge(
            c,
            xRight=innerX + innerW - badgePad,
            yTop=imageZoneTop - badgePad,
            text=uom,
        )

    textZoneTop = imageZoneBottom - imageTextGap
    cursorY = textZoneTop - 6  # slight shift down

    c.setFillColor(ink)
    cursorY -= drawParagraph(c, safeName, nameStyle, innerX, cursorY, innerW)
    cursorY -= textBetweenGap
    cursorY -= drawParagraph(c, priceText, priceStyle, innerX, cursorY, innerW)

    if isOffer:
        badgeW = min(120, innerW)
        badgeBoxH = 12
        badgeX = innerX + (innerW - badgeW) / 2
        badgeY = yBottom + 8

        c.setFillColor(accentGreen)
        c.roundRect(badgeX, badgeY, badgeW, badgeBoxH, 6, fill=1, stroke=0)
        drawParagraph(c, "SPECIAL", offerBadgeStyle, badgeX, badgeY + badgeBoxH - 2, badgeW)
        c.setFillColor(ink)


# ----------------------------
# Pagination helpers
# ----------------------------
def simulateCategoryPages(items: List[Dict[str, Any]]) -> int:
    return 1 if not items else int(math.ceil(len(items) / float(itemsPerPage)))


def drawCategoryPages(
    c: canvas.Canvas,
    items: List[Dict[str, Any]],
    isOfferSection: bool,
    brandName: str,
    brandRange: str,
    categoryName: str,
    pageNoStart: int,
    generatedOn: str,
) -> int:
    pageNo = pageNoStart
    startYTop = pageHeight - marginTop
    minY = marginBottom + footerReserved

    col1X = marginLeft
    col2X = marginLeft + columnWidth + gutter
    colXs = [col1X, col2X]

    def pageHeader() -> float:
        return drawBrandCategoryBar(c, brandName, brandRange, categoryName, startYTop)

    headerBottomY = pageHeader()
    available = headerBottomY - minY
    totalGaps = rowGap * (rowsPerPage - 1)
    cellHeight = (available - totalGaps) / float(rowsPerPage)
    cellHeight = max(cellHeight, 2.15 * inch)

    idx = 0
    while idx < len(items):
        pageChunk = items[idx: idx + itemsPerPage]

        y = headerBottomY
        for r in range(rowsPerPage):
            rowTop = y - (r * (cellHeight + rowGap))

            rowStart = r * columns
            rowItems = pageChunk[rowStart: rowStart + columns]
            if not rowItems:
                continue

            # ✅ Center last row if only 1 item in that row
            if len(rowItems) == 1:
                xPositions = [marginLeft + (contentWidth - columnWidth) / 2]
            else:
                xPositions = colXs

            for j, item in enumerate(rowItems):
                drawProductCardFixed(
                    c=c,
                    x=xPositions[j],
                    yTop=rowTop,
                    cardWidth=columnWidth,
                    cardHeight=cellHeight,
                    product=item,
                    isOffer=isOfferSection,
                )

        drawFooter(c, pageNo, generatedOn)

        idx += itemsPerPage
        if idx < len(items):
            c.showPage()
            pageNo += 1
            headerBottomY = pageHeader()

    return pageNo + 1


# ----------------------------
# Build PDF
# ----------------------------
def buildCatalog() -> None:
    ensureDir(imageCacheDir)

    generatedOn = datetime.now().strftime("%Y-%m-%d %H:%M")

    allProducts = loadProducts(jsonPath)
    if maxProductsTotal:
        allProducts = allProducts[:maxProductsTotal]

    # ✅ Threaded prefetch (ONLY addition)
    prefetchImages(allProducts, maxWorkers=12)

    offersHierarchy = buildHierarchy(allProducts, offerOnly=True)
    regularHierarchy = buildHierarchy(allProducts, offerOnly=False)

    def countTocRows(h: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> int:
        rows = 0
        for _, cats in h.items():
            rows += 1
            if len(cats) > tocShowCategoriesIfMoreThan:
                rows += len(cats)
        return rows

    offerRows = countTocRows(offersHierarchy)
    regularRows = countTocRows(regularHierarchy)

    tocTopY = pageHeight - marginTop
    tocHeaderH = (
        measureParagraphHeight("Super Asia Product Catalogue", titleStyle, contentWidth)
        + measureParagraphHeight("Table of Contents", tocTitleStyle, contentWidth)
        + 34
    )
    tocAvailable = (tocTopY - tocHeaderH) - (marginBottom + footerReserved)
    rowsPerTocPage = max(1, int(tocAvailable / 16))
    tocPages = int(math.ceil((2 + offerRows + regularRows) / float(rowsPerTocPage)))

    tocStartPage = 2
    contentStartPage = 1 + 1 + tocPages

    brandRanges: Dict[str, Dict[str, Tuple[int, int]]] = {"OFFER PRODUCTS": {}, "REGULAR PRODUCTS": {}}
    categoryRanges: Dict[str, Dict[str, Dict[str, Tuple[int, int]]]] = {"OFFER PRODUCTS": {}, "REGULAR PRODUCTS": {}}

    currentPage = contentStartPage

    def simulateSection(sectionTitle: str, hierarchy: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> None:
        nonlocal currentPage

        for brandName in sorted(hierarchy.keys(), key=lambda s: s.upper()):
            brandStart = currentPage
            categoryRanges[sectionTitle].setdefault(brandName, {})

            cats = hierarchy[brandName]
            for categoryName in sorted(cats.keys(), key=lambda s: s.upper()):
                catStart = currentPage
                pagesUsed = simulateCategoryPages(cats[categoryName])
                catEnd = catStart + pagesUsed - 1
                categoryRanges[sectionTitle][brandName][categoryName] = (catStart, catEnd)
                currentPage = catEnd + 1

            brandRanges[sectionTitle][brandName] = (brandStart, currentPage - 1)

    simulateSection("OFFER PRODUCTS", offersHierarchy)
    simulateSection("REGULAR PRODUCTS", regularHierarchy)

    c = canvas.Canvas(outputPdfPath, pagesize=LETTER)

    # Cover
    y = pageHeight - marginTop
    c.setFillColor(brandMaroon)
    c.rect(marginLeft, y - 0.35 * inch, contentWidth, 0.35 * inch, fill=1, stroke=0)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(marginLeft + 14, y - 0.35 * inch + 10, "Super Asia")
    c.setFont("Helvetica", 10)
    c.drawString(marginLeft + 120, y - 0.35 * inch + 12, "Product Catalogue")

    c.setFillColor(ink)
    y -= 0.55 * inch
    y -= drawParagraph(c, "Super Asia Product Catalogue", titleStyle, marginLeft, y, contentWidth) + 10
    y -= drawParagraph(c, "Offer Products and Regular Products", sectionStyle, marginLeft, y, contentWidth) + 18
    y -= drawParagraph(c, f"Generated {generatedOn}", smallGreyStyle, marginLeft, y, contentWidth) + 6
    y -= drawParagraph(c, "Paper Size: Letter (Canada)", smallGreyStyle, marginLeft, y, contentWidth) + 20
    drawFooter(c, 1, generatedOn)
    c.showPage()

    # TOC entries
    tocEntries: List[Tuple[str, str, float, bool]] = []

    def addTocSection(sectionTitle: str, hierarchy: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> None:
        tocEntries.append((sectionTitle, "", 0, True))

        for brandName in sorted(hierarchy.keys(), key=lambda s: s.upper()):
            bStart, bEnd = brandRanges[sectionTitle][brandName]
            tocEntries.append((brandName.title(), rangePlain(bStart, bEnd), 0, False))

            catNames = sorted(hierarchy[brandName].keys(), key=lambda s: s.upper())
            if len(catNames) > tocShowCategoriesIfMoreThan:
                for categoryName in catNames:
                    tocEntries.append((categoryName.title(), "", 18, False))

    addTocSection("OFFER PRODUCTS", offersHierarchy)
    addTocSection("REGULAR PRODUCTS", regularHierarchy)

    tocPageNo = tocStartPage
    tocRowIndex = 0

    while tocRowIndex < len(tocEntries):
        y = pageHeight - marginTop

        c.setFillColor(brandMaroon)
        c.rect(marginLeft, y - 0.28 * inch, contentWidth, 0.28 * inch, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(marginLeft + 10, y - 0.28 * inch + 7, "Super Asia")

        c.setFillColor(ink)
        y -= (0.28 * inch + 12)

        y -= drawParagraph(c, "Super Asia Product Catalogue", titleStyle, marginLeft, y, contentWidth) + 2
        y -= drawParagraph(c, "Table of Contents", tocTitleStyle, marginLeft, y, contentWidth) + 12

        c.setStrokeColor(ruleGrey)
        c.setLineWidth(0.8)
        c.line(marginLeft, y, marginLeft + contentWidth, y)
        y -= 18

        rowsThisPage = 0
        while tocRowIndex < len(tocEntries) and rowsThisPage < rowsPerTocPage:
            left, right, indent, isSection = tocEntries[tocRowIndex]

            if isSection:
                c.setFillColor(brandMaroon)
                c.setFont("Helvetica-Bold", 10)
                c.drawString(marginLeft, y, left)
                c.setFillColor(ink)
                y -= 16
            else:
                c.setFont("Helvetica-Bold" if indent == 0 else "Helvetica", 10)
                y -= drawTocRow(c, left, right, marginLeft, y, contentWidth, indent=indent)

            tocRowIndex += 1
            rowsThisPage += 1

        drawFooter(c, tocPageNo, generatedOn)
        tocPageNo += 1
        if tocRowIndex < len(tocEntries):
            c.showPage()

    c.showPage()

    # Content
    pageNo = contentStartPage

    def renderSection(sectionTitle: str, hierarchy: Dict[str, Dict[str, List[Dict[str, Any]]]], isOfferSection: bool) -> None:
        nonlocal pageNo

        for brandName in sorted(hierarchy.keys(), key=lambda s: s.upper()):
            bStart, bEnd = brandRanges[sectionTitle][brandName]
            brandRangeStr = rangeText(bStart, bEnd)

            cats = hierarchy[brandName]
            for categoryName in sorted(cats.keys(), key=lambda s: s.upper()):
                pageNo = drawCategoryPages(
                    c=c,
                    items=cats[categoryName],
                    isOfferSection=isOfferSection,
                    brandName=brandName,
                    brandRange=brandRangeStr,
                    categoryName=categoryName,
                    pageNoStart=pageNo,
                    generatedOn=generatedOn,
                )
                c.showPage()

    renderSection("OFFER PRODUCTS", offersHierarchy, True)
    renderSection("REGULAR PRODUCTS", regularHierarchy, False)

    c.save()

    print(f"✅ Catalog created: {outputPdfPath}")
    print(f"✅ Generated on: {generatedOn}")
    print(f"✅ Image cache: {imageCacheDir}")
    print(f"✅ TOC pages: {tocPages}")
    print(f"✅ Content starts at page: {contentStartPage}")


if __name__ == "__main__":
    buildCatalog()
