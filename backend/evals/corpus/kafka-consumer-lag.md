# Kafka consumer lag

Alert `OrderEventsLagHigh` fires when the order-events consumers fall more than 50,000 messages
behind.

## Check the lag per partition

    kafka-consumer-groups --bootstrap-server kafka:9092 --describe --group order-processor

If one partition is far behind and the others are not, look for a poison message: a record the
consumer crashes on, so it is retried forever.

## Poison messages

Find the stuck offset in the consumer logs, then move the record to the dead-letter topic
`order-events.dlq` and skip it. Never delete it: someone must replay it after the fix.

## Rebalance storms

If consumers keep joining and leaving the group, partitions are reassigned over and over and
nothing gets processed. The cause is usually processing that takes longer than
`max.poll.interval.ms`. Lower `max.poll.records` or raise the interval.

## Scaling

Add consumers only up to the number of partitions (24); extra consumers sit idle.
