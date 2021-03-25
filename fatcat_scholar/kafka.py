import sys
import json
import signal
from collections import Counter
from typing import List, Any

from confluent_kafka import Consumer, Producer, KafkaException


class KafkaWorker:
    """
    Base class for Scholar workers which consume from Kafka topics.

    Configuration (passed to __init__):

        kafka_brokers (List[str]): brokers to connect to

        consume_topics (List[str]): topics to consume from

        consumer_group (str): kafka consumer group

        batch_size (int): number of records to consume and process at a time

        batch_timeout_sec (int): max seconds for each batch to process. set to 0 to disable

    API:

        __init__()

        run()
            starts consuming, calling process_batch() for each message batch

        process_batch(batch: List[dict]) -> None
            implemented by sub-class

        process_msg(msg: dict) -> None
            implemented by sub-class

    Example of producing (in a worker):

        producer = self.create_kafka_producer(...)

        producer.produce(
            topic,
            some_obj.json(exclude_none=True).encode('UTF-8'),
            key=key,
            on_delivery=self._fail_fast_produce)

        # check for errors etc
        producer.poll(0)
    """

    def __init__(
        self,
        kafka_brokers: List[str],
        consume_topics: List[str],
        consumer_group: str,
        **kwargs: Any,
    ):
        self.counts: Counter = Counter()
        self.kafka_brokers = kafka_brokers
        self.batch_size = kwargs.get("batch_size", 1)
        self.batch_timeout_sec = kwargs.get("batch_timeout_sec", 60)
        self.poll_interval_sec = kwargs.get("poll_interval_sec", 5.0)
        self.consumer = self.create_kafka_consumer(
            kafka_brokers, consume_topics, consumer_group
        )

    @staticmethod
    def _fail_fast_produce(err: Any, msg: Any) -> None:
        if err is not None:
            print(f"Kafka producer delivery error: {err}", file=sys.stderr)
            raise KafkaException(err)

    @staticmethod
    def _timeout_handler(signum: Any, frame: Any) -> None:
        raise TimeoutError("timeout processing record")

    @staticmethod
    def create_kafka_consumer(
        kafka_brokers: List[str], consume_topics: List[str], consumer_group: str
    ) -> Consumer:
        """
        NOTE: it is important that consume_topics be str, *not* bytes
        """

        def _on_rebalance(consumer: Any, partitions: Any) -> None:

            for p in partitions:
                if p.error:
                    raise KafkaException(p.error)

            print(
                f"Kafka partitions rebalanced: {consumer} / {partitions}",
                file=sys.stderr,
            )

        def _fail_fast_consume(err: Any, partitions: Any) -> None:
            if err is not None:
                print(f"Kafka consumer commit error: {err}", file=sys.stderr)
                raise KafkaException(err)
            for p in partitions:
                # check for partition-specific commit errors
                if p.error:
                    print(
                        f"Kafka consumer commit error: {p.error}",
                        file=sys.stderr,
                    )
                    raise KafkaException(p.error)

        config = {
            "bootstrap.servers": ",".join(kafka_brokers),
            "group.id": consumer_group,
            "on_commit": _fail_fast_consume,
            # messages don't have offset marked as stored until processed,
            # but we do auto-commit stored offsets to broker
            "enable.auto.offset.store": False,
            "enable.auto.commit": True,
            # user code timeout; if no poll after this long, assume user code
            # hung and rebalance (default: 6min)
            "max.poll.interval.ms": 360000,
            "default.topic.config": {"auto.offset.reset": "latest"},
        }

        consumer = Consumer(config)
        consumer.subscribe(
            consume_topics, on_assign=_on_rebalance, on_revoke=_on_rebalance,
        )
        print(
            f"Consuming from kafka topics {consume_topics}, group {consumer_group}",
            file=sys.stderr,
        )
        return consumer

    @staticmethod
    def create_kafka_producer(kafka_brokers: List[str]) -> Producer:
        """
        This configuration is for large compressed messages.
        """

        config = {
            "bootstrap.servers": ",".join(kafka_brokers),
            "message.max.bytes": 30000000,  # ~30 MBytes; broker is ~50 MBytes
            "api.version.request": True,
            "api.version.fallback.ms": 0,
            "compression.codec": "gzip",
            "retry.backoff.ms": 250,
            "linger.ms": 1000,
            "batch.num.messages": 50,
            "delivery.report.only.error": True,
            "default.topic.config": {
                "message.timeout.ms": 30000,
                "request.required.acks": -1,  # all brokers must confirm
            },
        }
        return Producer(config)

    def run(self) -> Counter:

        if self.batch_timeout_sec:
            signal.signal(signal.SIGALRM, self._timeout_handler)

        while True:
            batch = self.consumer.consume(
                num_messages=self.batch_size, timeout=self.poll_interval_sec,
            )

            print(
                f"... got {len(batch)} kafka messages ({self.poll_interval_sec}sec poll interval). stats: {self.counts}",
                file=sys.stderr,
            )

            if not batch:
                continue

            # first check errors on entire batch...
            for msg in batch:
                if msg.error():
                    raise KafkaException(msg.error())

            # ... then process, with optional timeout
            self.counts["total"] += len(batch)
            records = [json.loads(msg.value().decode("utf-8")) for msg in batch]

            if self.batch_timeout_sec:
                signal.alarm(int(self.batch_timeout_sec))
                try:
                    self.process_batch(records)
                except TimeoutError as te:
                    raise te
                finally:
                    signal.alarm(0)
            else:
                self.process_batch(records)

            self.counts["processed"] += len(batch)

            # ... then record progress
            for msg in batch:
                # will be auto-commited by librdkafka from this "stored" value
                self.consumer.store_offsets(message=msg)

        # Note: never actually get here, but including as documentation on how to clean up
        self.consumer.close()
        return self.counts

    def process_batch(self, batch: List[dict]) -> None:
        """
        Workers can override this method for batch processing. By default it
        calls process_msg() for each message in the batch.
        """
        for msg in batch:
            self.process_msg(msg)

    def process_msg(self, msg: dict) -> None:
        """
        Workers can override this method for individual record processing.
        """
        raise NotImplementedError("implementation required")
