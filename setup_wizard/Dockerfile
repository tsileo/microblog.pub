FROM python:3.7
WORKDIR /app
ADD . /app
RUN pip install -r requirements.txt
LABEL maintainer="t@a4.io"
LABEL pub.microblog.oneshot=true
CMD ["python", "wizard.py"]
