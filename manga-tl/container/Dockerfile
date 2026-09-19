FROM python:3.11-slim

# Python 3.11, а не 3.13 с хоста: под 3.13 у CV-стека нет стабильных колёс.
# Это одна из причин, зачем здесь вообще контейнер.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PAGES_ROOT=/pages

# Словари всех исходных языков ставим сразу: они весят единицы мегабайт,
# а их нехватка вскрывается только на чужой главе, уже в бою. osd — не язык,
# а определитель ориентации и письменности, нужен вертикальному тексту.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-kor \
        tesseract-ocr-chi-sim \
        tesseract-ocr-chi-tra \
        tesseract-ocr-fra \
        tesseract-ocr-deu \
        tesseract-ocr-spa \
        tesseract-ocr-ita \
        tesseract-ocr-por \
        tesseract-ocr-osd \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
