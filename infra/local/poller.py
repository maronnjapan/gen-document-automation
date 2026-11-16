"""ローカル開発用のSQSポーラー（本番では使わない）。

本番では Lambda のイベントソースマッピングが SQS から通知を取り出して関数を起動し、受信回数の上限を超えた通知は DLQ へ移る。
ローカルではこのスクリプトが generation-queue / review-queue と、それぞれの DLQ を監視して同じ形式のイベントでハンドラーを呼び出す。
起動時に S3 互換ストレージのバケットと、DLQ 付きの FIFO キューを作成する（作成済みなら何もしない）。

呼び出し先（GENERATION_TARGET / REVIEW_TARGET / DEAD_LETTER_TARGET）:
- http(s)://...   Lambda Runtime Interface Emulator のURL（Lambda用コンテナイメージをそのまま使う）
- module:func     backend/ のモジュールを同じプロセスで呼び出す（既定）
"""
import importlib
import json
import os
import pathlib
import sys
import time
import urllib.request

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "backend"))
SQS, S3 = boto3.client("sqs"), boto3.client("s3")
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", 5))  # 失敗した通知を再配信するまでの秒数（本番はキューの可視性タイムアウト）
MAX_RECEIVE_COUNT = os.environ.get("MAX_RECEIVE_COUNT", "3")


def dlq_url(url):
    return url.replace("-queue.fifo", "-dlq.fifo")


def bootstrap():
    try:
        S3.create_bucket(Bucket=os.environ["S3_BUCKET"], CreateBucketConfiguration={"LocationConstraint": os.environ["AWS_DEFAULT_REGION"]})
    except (S3.exceptions.BucketAlreadyOwnedByYou, S3.exceptions.BucketAlreadyExists):
        pass
    for env in ("GENERATION_QUEUE_URL", "REVIEW_QUEUE_URL"):
        name = os.environ[env].rstrip("/").rsplit("/", 1)[-1]
        dlq = SQS.create_queue(QueueName=name.replace("-queue.fifo", "-dlq.fifo"), Attributes={"FifoQueue": "true"})["QueueUrl"]
        arn = SQS.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        redrive = json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": MAX_RECEIVE_COUNT})  # 本番と同じく上限を超えたらDLQへ
        SQS.create_queue(QueueName=name, Attributes={"FifoQueue": "true", "VisibilityTimeout": "60", "RedrivePolicy": redrive})


def invoke(target, event):
    if target.startswith("http"):
        req = urllib.request.Request(target, data=json.dumps(event).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=960) as res:
            result = json.load(res)
        if isinstance(result, dict) and "errorType" in result:  # 関数が例外で終了した
            raise RuntimeError(f"{result['errorType']}: {result.get('errorMessage')}")
        return result
    module, func = target.split(":")
    return getattr(importlib.import_module(module), func)(event, None)


def poll_once(url, target):
    res = SQS.receive_message(QueueUrl=url, MaxNumberOfMessages=1, WaitTimeSeconds=1)
    for m in res.get("Messages", []):
        record = {"messageId": m["MessageId"], "receiptHandle": m["ReceiptHandle"], "body": m["Body"], "eventSource": "aws:sqs", "eventSourceARN": url}
        try:
            invoke(target, {"Records": [record]})
            SQS.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
            print(f"[poller] {url.rsplit('/', 1)[-1]} {m['Body']} -> done", flush=True)
        except Exception as e:  # 例外は再配信させる（受信回数の上限を超えるとDLQへ移る）
            SQS.change_message_visibility(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=RETRY_DELAY)
            print(f"[poller] {url.rsplit('/', 1)[-1]} {m['Body']} -> retry ({e})", flush=True)


def main():
    for _ in range(30):  # 依存サービスの起動待ち
        try:
            bootstrap()
            break
        except Exception as e:
            print(f"[poller] waiting for storage/queue services: {e}", flush=True)
            time.sleep(2)
    else:
        sys.exit("storage/queue services are not available")
    generation, review = os.environ["GENERATION_QUEUE_URL"], os.environ["REVIEW_QUEUE_URL"]
    dead_letter = os.environ.get("DEAD_LETTER_TARGET", "common:dead_letter")
    targets = {generation: os.environ.get("GENERATION_TARGET", "generator:handler"), review: os.environ.get("REVIEW_TARGET", "reviewer:handler"),
               dlq_url(generation): dead_letter, dlq_url(review): dead_letter}
    print(f"[poller] watching {list(targets)}", flush=True)
    while True:
        for url, target in targets.items():
            poll_once(url, target)


if __name__ == "__main__":
    main()
