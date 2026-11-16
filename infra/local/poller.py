"""ローカル開発用のSQSポーラー（本番では使わない）。

本番では Lambda のイベントソースマッピングが SQS から通知を取り出して関数を起動する。ローカルではこのスクリプトが代わりに
generation-queue / review-queue を監視し、同じ形式のイベント（部分バッチ応答つき）でハンドラーを呼び出す。
起動時にS3互換ストレージのバケットと、DLQ付きのキューを作成する（作成済みなら何もしない）。

呼び出し先（GENERATION_TARGET / REVIEW_TARGET）:
- http(s)://...                      Lambda Runtime Interface Emulator のURL（Lambda用コンテナイメージをそのまま使う）
- generator:handler（既定）          backend/ のモジュールを同じプロセスで呼び出す
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


def bootstrap():
    try:
        S3.create_bucket(Bucket=os.environ["S3_BUCKET"], CreateBucketConfiguration={"LocationConstraint": os.environ["AWS_DEFAULT_REGION"]})
    except (S3.exceptions.BucketAlreadyOwnedByYou, S3.exceptions.BucketAlreadyExists):
        pass
    for env in ("GENERATION_QUEUE_URL", "REVIEW_QUEUE_URL"):
        name = os.environ[env].rstrip("/").rsplit("/", 1)[-1]
        dlq = SQS.create_queue(QueueName=f"{name}-dlq")["QueueUrl"]
        dlq_arn = SQS.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        redrive = json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"})  # 本番と同じく最大受信回数を超えたらDLQへ
        SQS.create_queue(QueueName=name, Attributes={"VisibilityTimeout": "60", "RedrivePolicy": redrive})


def invoke(target, event):
    if target.startswith("http"):
        req = urllib.request.Request(target, data=json.dumps(event).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=960) as res:
            result = json.load(res)
        if isinstance(result, dict) and "errorType" in result:  # 関数がエラー終了した
            raise RuntimeError(f"{result['errorType']}: {result.get('errorMessage')}")
        return result
    module, func = target.split(":")
    return getattr(importlib.import_module(module), func)(event, None)


def poll_once(url, target):
    res = SQS.receive_message(QueueUrl=url, MaxNumberOfMessages=1, WaitTimeSeconds=1, MessageSystemAttributeNames=["ApproximateReceiveCount"])
    for m in res.get("Messages", []):
        record = {"messageId": m["MessageId"], "receiptHandle": m["ReceiptHandle"], "body": m["Body"], "attributes": m.get("Attributes", {}),
                  "eventSource": "aws:sqs", "eventSourceARN": url}
        try:
            failed = {f["itemIdentifier"] for f in (invoke(target, {"Records": [record]}) or {}).get("batchItemFailures", [])}
        except Exception as e:  # 関数自体の失敗はバッチ全体の失敗として再配信する
            print(f"[poller] invoke failed: {e}", flush=True)
            failed = {m["MessageId"]}
        print(f"[poller] {url.rsplit('/', 1)[-1]} {m['Body']} -> {'retry' if failed else 'done'}", flush=True)
        if failed:
            SQS.change_message_visibility(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=RETRY_DELAY)
        else:
            SQS.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    return bool(res.get("Messages"))


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
    targets = {os.environ["GENERATION_QUEUE_URL"]: os.environ.get("GENERATION_TARGET", "generator:handler"),
               os.environ["REVIEW_QUEUE_URL"]: os.environ.get("REVIEW_TARGET", "reviewer:handler")}
    print(f"[poller] watching {list(targets)}", flush=True)
    while True:
        for url, target in targets.items():
            poll_once(url, target)


if __name__ == "__main__":
    main()
