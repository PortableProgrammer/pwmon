FROM python:slim
RUN apt-get update && apt-get install vim -y && apt-get install git -y
RUN python3 -m pip install --upgrade pip

ADD ./requirements.txt /tmp
RUN python3 -m pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

ADD ./pwmon.py /
CMD ["/pwmon.py"]
