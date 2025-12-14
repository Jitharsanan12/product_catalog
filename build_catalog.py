import os
import io
import json
from typing import Dict, Any, List, Tuple, Optional

import requests
from PIL import Image as PILImage

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph


# =========================
# CONFIG
# =========================
baseDir = os.path.dirname(os.path.abspath(__file__))
jsonPath = os.path.join(baseDir, "products.json")
outputPdfPath = os.path.join(baseDir, "catalog.pdf")

imageUrlTemplate = "https://superasia.ca/web/image/product.product/{productId}/image_512"
imageCacheDir = os.path.join(baseDir, "_image_cache")

# Testing limit (set None for all)
maxProductsTotal = None  # e.g. 200

httpTimeoutSeconds = 20
httpRetries = 3
httpHeaders = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
}


# =========================
# PAGE LAYOUT (LETTER)
# =========================
pageWidth, pageHeight = LETTER

marginLeft = 0.5 * inch
marginRight = 0.5 * inch
marginTop = 0.6 * inch
marginBottom = 0.6 * inch

gutter = 0.35 * inch
columns = 2

contentWidth = pageWidth - marginLeft - marginRight
columnWidth = (contentWidth - gutter) / columns

cardPadding = 8
cardInnerGap = 6
rowGap = 16
sectionGap = 14
headerGap = 10

imageTargetWidth = columnWidth - (cardPadding * 2)
imageMaxHeight = 2.1 * inch


# =========================
# STYLES
# =========================
titleStyle = ParagraphStyle("TitleStyle", fontName="Helvetica-Bold", fontSize=16, leading=18)
sectionStyle = ParagraphStyle("SectionStyle", fontName="Helvetica-Bold", fontSize=14, leading=16)
categoryStyle = ParagraphStyle("CategoryStyle", fontName="Helvetica-Bold", fontSize=18, leading=20, alignment=1)
categoryPathStyle = ParagraphStyle("CategoryPathStyle", fontName="Helvetica", fontSize=9, leading=11, textColor=colors.grey)

nameStyle = ParagraphStyle("NameStyle", fontName="Helvetica-Bold", fontSize=10, leading=12, alignment=1)
uomStyle = ParagraphStyle("UomStyle", fontName="Helvetica", fontSize=9, leading=11, alignment=1)
priceStyle = ParagraphStyle("PriceStyle", fontName="Helvetica", fontSize=9, leading=11, alignment=1)
offerBadgeStyle = ParagraphStyle("OfferBadgeStyle", fontName="Helvetica-Bold", fontSize=9, leading=11, alignment=1, textColor=colors.white)


# =========================
# UTIL
# =========================
def ensureDir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def parseFloatSafe(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except:
        return None

def money(value: Any) -> Optional[float]:
    v = parseFloatSafe(value)
    if v is None:
        return None
    return round(v + 1e-9, 2)


# =========================
# DATA
# =========================
def loadProducts(jsonFilePath: str) -> List[Dict[str, Any]]:
    with open(jsonFilePath, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, dict):
        return list(raw.values())
    if isinstance(raw, list):
        return raw

    raise ValueError("Unexpected JSON structure (must be dict or list)")


def getSecondCategory(categoryStr: str) -> str:
    # "All / Bakery / Cookies" => "Bakery"
    if not categoryStr:
        return "Other"
    parts = [p.strip() for p in categoryStr.split("/") if p.strip()]
    return parts[1] if len(parts) >= 2 else parts[0]


def minPriceListPrice(product: Dict[str, Any]) -> Optional[float]:
    lowest = None
    for tier in product.get("priceList", []) or []:
        fp = money(tier.get("fixedPrice"))
        if fp is None:
            continue
        if lowest is None or fp < lowest:
            lowest = fp
    return lowest


# =========================
# OFFER RULE
# =========================
def isOfferProduct(product: Dict[str, Any]) -> bool:
    """
    OFFER if: min(priceList.fixedPrice) < regularPrice
    """
    reg = money(product.get("regularPrice"))
    if reg is None:
        reg = money(product.get("price"))
    if reg is None:
        return False

    lowest = minPriceListPrice(product)
    if lowest is None:
        return False

    return lowest < reg


def getOfferPrice(product: Dict[str, Any]) -> Optional[float]:
    if not isOfferProduct(product):
        return None
    return minPriceListPrice(product)


def groupBySecondCategory(products: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}

    for p in products:
        catName = getSecondCategory(str(p.get("category", "")))
        grouped.setdefault(catName, []).append(p)

    for catName in grouped:
        grouped[catName].sort(key=lambda x: str(x.get("name", "")).upper())

    return grouped


# =========================
# IMAGE (CACHE + RETRIES)
# =========================
def getCachedImagePath(productId: int) -> str:
    ensureDir(imageCacheDir)
    return os.path.join(imageCacheDir, f"{productId}.jpg")

def downloadImageToCache(productId: int) -> Optional[str]:
    localPath = getCachedImagePath(productId)

    if os.path.exists(localPath) and os.path.getsize(localPath) > 2000:
        return localPath

    url = imageUrlTemplate.format(productId=productId)

    for _ in range(httpRetries):
        try:
            r = requests.get(url, timeout=httpTimeoutSeconds, headers=httpHeaders)
            if r.status_code != 200 or not r.content:
                continue

            img = PILImage.open(io.BytesIO(r.content))
            img = img.convert("RGB")
            img.save(localPath, "JPEG", quality=90)
            return localPath
        except:
            continue

    return None


def computeScaledImageSize(localImagePath: str, targetWidth: float, maxHeight: float) -> Tuple[float, float]:
    img = PILImage.open(localImagePath)
    w, h = img.size

    if w <= 0 or h <= 0:
        return targetWidth, min(maxHeight, targetWidth)

    scale = targetWidth / float(w)
    newW = targetWidth
    newH = float(h) * scale

    if newH > maxHeight:
        scale2 = maxHeight / newH
        newH = maxHeight
        newW = newW * scale2

    return newW, newH


# =========================
# TEXT
# =========================
def measureParagraphHeight(text: str, style: ParagraphStyle, width: float) -> float:
    p = Paragraph(text, style)
    _, h = p.wrap(width, 10000)
    return h

def drawParagraph(c: canvas.Canvas, text: str, style: ParagraphStyle, x: float, yTop: float, width: float) -> float:
    p = Paragraph(text, style)
    _, h = p.wrap(width, 10000)
    p.drawOn(c, x, yTop - h)
    return h


# =========================
# PRICE LINES
# =========================
def buildPriceLinesForOffer(product: Dict[str, Any]) -> List[str]:
    reg = money(product.get("regularPrice"))
    if reg is None:
        reg = money(product.get("price"))

    offer = getOfferPrice(product)

    lines: List[str] = []
    if reg is not None:
        lines.append(f"Regular: ${reg:,.2f}")
    if offer is not None:
        lines.append(f"<b>NOW: ${offer:,.2f}</b>")

    for tier in product.get("priceList", []) or []:
        mq = tier.get("minQuantity")
        fp = money(tier.get("fixedPrice"))
        note = tier.get("note")
        if mq is None or fp is None:
            continue
        line = f"{mq}+ : ${fp:,.2f}"
        if note:
            line += f" ({note})"
        lines.append(line)

    if not lines:
        lines.append("Price: N/A")

    return lines


def buildPriceLinesForRegular(product: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for tier in product.get("priceList", []) or []:
        mq = tier.get("minQuantity")
        fp = money(tier.get("fixedPrice"))
        note = tier.get("note")
        if mq is None or fp is None:
            continue
        line = f"{mq}+ : ${fp:,.2f}"
        if note:
            line += f" ({note})"
        lines.append(line)

    if not lines:
        base = money(product.get("price"))
        lines.append(f"Price: ${base:,.2f}" if base is not None else "Price: N/A")

    return lines


# =========================
# PRODUCT CARD
# =========================
def computeCardHeight(product: Dict[str, Any], isOffer: bool) -> Tuple[float, Dict[str, Any]]:
    productId = int(product.get("id") or 0)
    name = str(product.get("name") or "").strip()
    uom = str(product.get("uom") or "").strip()

    localImagePath = downloadImageToCache(productId) if productId else None

    if localImagePath:
        imgW, imgH = computeScaledImageSize(localImagePath, imageTargetWidth, imageMaxHeight)
    else:
        imgW = imageTargetWidth
        imgH = min(imageMaxHeight, 1.6 * inch)

    textWidth = columnWidth - (cardPadding * 2)

    nameH = measureParagraphHeight(name or "&nbsp;", nameStyle, textWidth)
    uomH = measureParagraphHeight(uom, uomStyle, textWidth) if uom else 0

    lines = buildPriceLinesForOffer(product) if isOffer else buildPriceLinesForRegular(product)
    priceText = "<br/>".join(lines)
    priceH = measureParagraphHeight(priceText, priceStyle, textWidth)

    badgeH = 14 if isOffer else 0

    cardHeight = (
        cardPadding + imgH + cardInnerGap +
        nameH + (2 if uom else 0) + uomH +
        cardInnerGap + priceH +
        (8 if isOffer else 0) + badgeH +
        cardPadding
    )

    return cardHeight, {
        "name": name,
        "uom": uom,
        "localImagePath": localImagePath,
        "imgW": imgW,
        "imgH": imgH,
        "priceText": priceText,
        "isOffer": isOffer,
    }


def drawProductCard(c: canvas.Canvas, x: float, yTop: float, cardWidth: float, cardHeight: float, info: Dict[str, Any]) -> None:
    c.setStrokeColor(colors.lightgrey)
    c.setLineWidth(0.6)
    c.roundRect(x, yTop - cardHeight, cardWidth, cardHeight, 8, stroke=1, fill=0)

    innerX = x + cardPadding
    innerW = cardWidth - (cardPadding * 2)
    cursorY = yTop - cardPadding

    imgH = info["imgH"]
    imgW = info["imgW"]
    imgX = innerX + (innerW - imgW) / 2
    imgY = cursorY - imgH

    local = info["localImagePath"]
    if local and os.path.exists(local):
        c.drawImage(local, imgX, imgY, width=imgW, height=imgH, preserveAspectRatio=True)
    else:
        c.setFillColor(colors.whitesmoke)
        c.setStrokeColor(colors.lightgrey)
        c.rect(imgX, imgY, imgW, imgH, fill=1, stroke=1)
        c.setFillColor(colors.grey)
        c.setFont("Helvetica", 8)
        c.drawCentredString(imgX + imgW / 2, imgY + imgH / 2, "No Image")

    cursorY = imgY - cardInnerGap
    cursorY -= drawParagraph(c, info["name"] or "&nbsp;", nameStyle, innerX, cursorY, innerW)

    if info["uom"]:
        cursorY -= 2
        cursorY -= drawParagraph(c, info["uom"], uomStyle, innerX, cursorY, innerW)

    cursorY -= cardInnerGap
    cursorY -= drawParagraph(c, info["priceText"], priceStyle, innerX, cursorY, innerW)

    if info["isOffer"]:
        cursorY -= 8
        badgeW = min(90, innerW)
        badgeH = 14
        badgeX = innerX + (innerW - badgeW) / 2
        badgeY = cursorY - badgeH

        c.setFillColor(colors.red)
        c.roundRect(badgeX, badgeY, badgeW, badgeH, 6, fill=1, stroke=0)
        drawParagraph(c, "OFFER", offerBadgeStyle, badgeX, badgeY + badgeH - 2, badgeW)


# =========================
# HEADERS
# =========================
def drawSectionHeader(c: canvas.Canvas, sectionTitle: str) -> float:
    y = pageHeight - marginTop
    drawParagraph(c, "Super Asia Product Catalogue", titleStyle, marginLeft, y, contentWidth)
    y -= 22
    drawParagraph(c, sectionTitle, sectionStyle, marginLeft, y, contentWidth)
    y -= sectionGap
    return y

def drawCategoryHeader(c: canvas.Canvas, categoryName: str, yStart: float) -> float:
    y = yStart
    drawParagraph(c, categoryName.upper(), categoryStyle, marginLeft, y, contentWidth)
    y -= 26
    y -= headerGap
    return y


# =========================
# LAYOUT
# =========================
def renderCategoryOnCurrentPage(
    c: canvas.Canvas,
    sectionTitle: str,
    categoryName: str,
    items: List[Dict[str, Any]],
    isOfferSection: bool,
) -> None:
    # IMPORTANT: Do NOT showPage() here.
    # Caller decides when a new page starts.
    y = drawSectionHeader(c, sectionTitle)
    y = drawCategoryHeader(c, categoryName, y)

    currentY = y
    minY = marginBottom

    i = 0
    while i < len(items):
        rowItems = items[i:i + 2]

        heights = []
        infos = []
        for p in rowItems:
            h, info = computeCardHeight(p, isOffer=isOfferSection)
            heights.append(h)
            infos.append(info)

        rowHeight = max(heights)
        if (currentY - rowHeight) < minY:
            # New page within same category (overflow)
            c.showPage()
            y = drawSectionHeader(c, sectionTitle)
            y = drawCategoryHeader(c, categoryName, y)
            currentY = y

        col1X = marginLeft
        col2X = marginLeft + columnWidth + gutter

        if len(rowItems) == 1:
            singleX = marginLeft + (contentWidth - columnWidth) / 2
            drawProductCard(c, singleX, currentY, columnWidth, heights[0], infos[0])
        else:
            drawProductCard(c, col1X, currentY, columnWidth, heights[0], infos[0])
            drawProductCard(c, col2X, currentY, columnWidth, heights[1], infos[1])

        currentY -= (rowHeight + rowGap)
        i += len(rowItems)


# =========================
# MAIN
# =========================
def buildCatalog() -> None:
    ensureDir(imageCacheDir)

    allProducts = loadProducts(jsonPath)

    allOffers = [p for p in allProducts if isOfferProduct(p)]
    allRegular = [p for p in allProducts if not isOfferProduct(p)]

    allOffers.sort(key=lambda p: (getSecondCategory(str(p.get("category", ""))).upper(), str(p.get("name", "")).upper()))
    allRegular.sort(key=lambda p: (getSecondCategory(str(p.get("category", ""))).upper(), str(p.get("name", "")).upper()))

    if maxProductsTotal is not None:
        selectedOffers = allOffers[:maxProductsTotal]
        remaining = maxProductsTotal - len(selectedOffers)
        selectedRegular = allRegular[:max(0, remaining)]
    else:
        selectedOffers = allOffers
        selectedRegular = allRegular

    offersGrouped = groupBySecondCategory(selectedOffers)
    regularGrouped = groupBySecondCategory(selectedRegular)

    offerCats = sorted(offersGrouped.keys(), key=lambda s: s.upper())
    regularCats = sorted(regularGrouped.keys(), key=lambda s: s.upper())

    c = canvas.Canvas(outputPdfPath, pagesize=LETTER)

    # Cover (page 1)
    y = pageHeight - marginTop
    drawParagraph(c, "Super Asia Product Catalogue", titleStyle, marginLeft, y, contentWidth)
    y -= 40
    drawParagraph(c, "Offer Products + Regular Products", sectionStyle, marginLeft, y, contentWidth)
    y -= 18
    drawParagraph(c, "Paper: Letter (Canada)", categoryPathStyle, marginLeft, y, contentWidth)

    # Move to next page (page 2) for content
    c.showPage()

    # OFFERS (each category starts new page, but DO NOT double showPage)
    if not offerCats:
        y = drawSectionHeader(c, "OFFER PRODUCTS")
        drawParagraph(c, "No offer products found (min(priceList) < regularPrice).", categoryPathStyle, marginLeft, y, contentWidth)
        c.showPage()
    else:
        first = True
        for cat in offerCats:
            if not first:
                c.showPage()
            renderCategoryOnCurrentPage(
                c=c,
                sectionTitle="OFFER PRODUCTS",
                categoryName=cat,
                items=offersGrouped[cat],
                isOfferSection=True,
            )
            first = False

        # after offers, start regular section on a fresh page
        c.showPage()

    # REGULAR
    if not regularCats:
        y = drawSectionHeader(c, "REGULAR PRODUCTS")
        drawParagraph(c, "No regular products found in selected sample.", categoryPathStyle, marginLeft, y, contentWidth)
        c.showPage()
    else:
        first = True
        for cat in regularCats:
            if not first:
                c.showPage()
            renderCategoryOnCurrentPage(
                c=c,
                sectionTitle="REGULAR PRODUCTS",
                categoryName=cat,
                items=regularGrouped[cat],
                isOfferSection=False,
            )
            first = False

    c.save()

    print(f"✅ Catalog created: {outputPdfPath}")
    print(f"   Offers used:  {len(selectedOffers)}")
    print(f"   Regular used: {len(selectedRegular)}")
    print(f"   Total used:   {len(selectedOffers) + len(selectedRegular)}")
    print(f"   Cache folder: {imageCacheDir}")


if __name__ == "__main__":
    buildCatalog()
