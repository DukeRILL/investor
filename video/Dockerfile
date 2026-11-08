FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-montserrat \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY video/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ADD https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx /app/models/
COPY video/app.py video/render.py video/catalog.py video/planner.py video/stock.py ./
COPY prompts/video_scenarios.md /app/prompts/video_scenarios.md
ENV DATA_DIR=/data
EXPOSE 8020
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8020"]
