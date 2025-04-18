import os.path as p
import random
import threading
import time
from random import randrange

import pytest

from helpers.cluster import ClickHouseCluster
from helpers.postgres_utility import (
    PostgresManager,
    assert_nested_table_is_created,
    assert_number_of_columns,
    check_several_tables_are_synchronized,
    check_tables_are_synchronized,
    create_postgres_schema,
    create_postgres_table,
    create_replication_slot,
    drop_postgres_schema,
    drop_postgres_table,
    drop_replication_slot,
    get_postgres_conn,
    postgres_table_template,
    postgres_table_template_2,
    postgres_table_template_3,
    postgres_table_template_4,
    queries,
)
from helpers.test_tools import TSV, assert_eq_with_retry

cluster = ClickHouseCluster(__file__)
instance = cluster.add_instance(
    "instance",
    main_configs=["configs/log_conf.xml"],
    user_configs=["configs/users.xml"],
    with_postgres=True,
    stay_alive=True,
)

pg_manager = PostgresManager()


@pytest.fixture(scope="module")
def started_cluster():
    try:
        cluster.start()
        pg_manager.init(
            instance,
            cluster.postgres_ip,
            cluster.postgres_port,
            default_database="postgres_database",
        )
        yield cluster

    finally:
        cluster.shutdown()


@pytest.fixture(autouse=True)
def setup_teardown():
    print("PostgreSQL is available - running test")
    yield  # run test
    pg_manager.restart()


def test_load_and_sync_all_database_tables(started_cluster):
    NUM_TABLES = 5
    pg_manager.create_and_fill_postgres_tables(NUM_TABLES)
    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip, port=started_cluster.postgres_port
    )
    check_several_tables_are_synchronized(instance, NUM_TABLES)
    result = instance.query(
        "SELECT count() FROM system.tables WHERE database = 'test_database';"
    )
    assert int(result) == NUM_TABLES


def test_replicating_dml(started_cluster):
    NUM_TABLES = 5

    for i in range(NUM_TABLES):
        pg_manager.create_postgres_table(f"postgresql_replica_{i}")
        instance.query(
            "INSERT INTO postgres_database.postgresql_replica_{} SELECT number, {} from numbers(50)".format(
                i, i
            )
        )

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip, port=started_cluster.postgres_port
    )

    for i in range(NUM_TABLES):
        instance.query(
            f"INSERT INTO postgres_database.postgresql_replica_{i} SELECT 50 + number, {i} from numbers(1000)"
        )
    check_several_tables_are_synchronized(instance, NUM_TABLES)

    for i in range(NUM_TABLES):
        pg_manager.execute(
            f"UPDATE postgresql_replica_{i} SET value = {i} * {i} WHERE key < 50;"
        )
        pg_manager.execute(
            f"UPDATE postgresql_replica_{i} SET value = {i} * {i} * {i} WHERE key >= 50;"
        )

    check_several_tables_are_synchronized(instance, NUM_TABLES)

    for i in range(NUM_TABLES):
        pg_manager.execute(
            f"DELETE FROM postgresql_replica_{i} WHERE (value*value + {i}) % 2 = 0;"
        )
        pg_manager.execute(
            f"UPDATE postgresql_replica_{i} SET value = value - (value % 7) WHERE key > 128 AND key < 512;"
        )
        pg_manager.execute(f"DELETE FROM postgresql_replica_{i} WHERE key % 7 = 1;")

    check_several_tables_are_synchronized(instance, NUM_TABLES)


def test_different_data_types(started_cluster):
    conn = get_postgres_conn(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        database=True,
    )
    cursor = conn.cursor()
    cursor.execute("drop table if exists test_data_types;")
    cursor.execute("drop table if exists test_array_data_type;")

    cursor.execute(
        """CREATE TABLE test_data_types (
        id integer PRIMARY KEY, a smallint, b integer, c bigint, d real, e double precision, f serial, g bigserial,
        h timestamp, i date, j decimal(5, 5), k numeric(5, 5))"""
    )

    cursor.execute(
        """CREATE TABLE test_array_data_type
           (
                key Integer NOT NULL PRIMARY KEY,
                a Date[] NOT NULL,                          -- Date
                b Timestamp[] NOT NULL,                     -- DateTime64(6)
                c real[][] NOT NULL,                        -- Float32
                d double precision[][] NOT NULL,            -- Float64
                e decimal(5, 5)[][][] NOT NULL,             -- Decimal32
                f integer[][][] NOT NULL,                   -- Int32
                g Text[][][][][] NOT NULL,                  -- String
                h Integer[][][],                            -- Nullable(Int32)
                i Char(2)[][][][],                          -- Nullable(String)
                k Char(2)[]                                 -- Nullable(String)
           )"""
    )

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip, port=started_cluster.postgres_port
    )

    for i in range(10):
        instance.query(
            """
            INSERT INTO postgres_database.test_data_types VALUES
            ({}, -32768, -2147483648, -9223372036854775808, 1.12345, 1.1234567890, 2147483647, 9223372036854775807, '2000-05-12 12:12:12.012345', '2000-05-12', 0.2, 0.2)""".format(
                i
            )
        )

    check_tables_are_synchronized(instance, "test_data_types", "id")
    result = instance.query(
        "SELECT * FROM test_database.test_data_types ORDER BY id LIMIT 1;"
    )
    assert (
        result
        == "0\t-32768\t-2147483648\t-9223372036854775808\t1.12345\t1.123456789\t2147483647\t9223372036854775807\t2000-05-12 12:12:12.012345\t2000-05-12\t0.2\t0.2\n"
    )

    for i in range(10):
        col = random.choice(["a", "b", "c"])
        cursor.execute("UPDATE test_data_types SET {} = {};".format(col, i))
        cursor.execute("UPDATE test_data_types SET i = '2020-12-12';")

    check_tables_are_synchronized(instance, "test_data_types", "id")

    instance.query(
        "INSERT INTO postgres_database.test_array_data_type "
        "VALUES ("
        "0, "
        "['2000-05-12', '2000-05-12'], "
        "['2000-05-12 12:12:12.012345', '2000-05-12 12:12:12.012345'], "
        "[[1.12345], [1.12345], [1.12345]], "
        "[[1.1234567891], [1.1234567891], [1.1234567891]], "
        "[[[0.11111, 0.11111]], [[0.22222, 0.22222]], [[0.33333, 0.33333]]], "
        "[[[1, 1], [1, 1]], [[3, 3], [3, 3]], [[4, 4], [5, 5]]], "
        "[[[[['winx', 'winx', 'winx']]]]], "
        "[[[1, NULL], [NULL, 1]], [[NULL, NULL], [NULL, NULL]], [[4, 4], [5, 5]]], "
        "[[[[NULL]]]], "
        "[]"
        ")"
    )

    expected = (
        "0\t"
        + "['2000-05-12','2000-05-12']\t"
        + "['2000-05-12 12:12:12.012345','2000-05-12 12:12:12.012345']\t"
        + "[[1.12345],[1.12345],[1.12345]]\t"
        + "[[1.1234567891],[1.1234567891],[1.1234567891]]\t"
        + "[[[0.11111,0.11111]],[[0.22222,0.22222]],[[0.33333,0.33333]]]\t"
        "[[[1,1],[1,1]],[[3,3],[3,3]],[[4,4],[5,5]]]\t"
        "[[[[['winx','winx','winx']]]]]\t"
        "[[[1,NULL],[NULL,1]],[[NULL,NULL],[NULL,NULL]],[[4,4],[5,5]]]\t"
        "[[[[NULL]]]]\t"
        "[]\n"
    )

    check_tables_are_synchronized(instance, "test_array_data_type")
    result = instance.query(
        "SELECT * FROM test_database.test_array_data_type ORDER BY key;"
    )
    assert result == expected

    pg_manager.drop_materialized_db()
    cursor.execute("drop table if exists test_data_types;")
    cursor.execute("drop table if exists test_array_data_type;")


def test_load_and_sync_subset_of_database_tables(started_cluster):
    NUM_TABLES = 10
    pg_manager.create_and_fill_postgres_tables(NUM_TABLES)

    publication_tables = ""
    for i in range(NUM_TABLES):
        if i < int(NUM_TABLES / 2):
            if publication_tables != "":
                publication_tables += ", "
            publication_tables += f"postgresql_replica_{i}"

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        settings=[
            "materialized_postgresql_tables_list = '{}'".format(publication_tables)
        ],
    )

    time.sleep(1)

    for i in range(int(NUM_TABLES / 2)):
        table_name = f"postgresql_replica_{i}"
        assert_nested_table_is_created(instance, table_name)

    result = instance.query(
        """SELECT count() FROM system.tables WHERE database = 'test_database';"""
    )
    assert int(result) == int(NUM_TABLES / 2)

    database_tables = instance.query("SHOW TABLES FROM test_database")
    for i in range(NUM_TABLES):
        table_name = "postgresql_replica_{}".format(i)
        if i < int(NUM_TABLES / 2):
            assert table_name in database_tables
        else:
            assert table_name not in database_tables
        instance.query(
            "INSERT INTO postgres_database.{} SELECT 50 + number, {} from numbers(100)".format(
                table_name, i
            )
        )

    for i in range(NUM_TABLES):
        table_name = f"postgresql_replica_{i}"
        if i < int(NUM_TABLES / 2):
            check_tables_are_synchronized(instance, table_name)


def test_changing_replica_identity_value(started_cluster):
    pg_manager.create_postgres_table("postgresql_replica")
    instance.query(
        "INSERT INTO postgres_database.postgresql_replica SELECT 50 + number, number from numbers(50)"
    )

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip, port=started_cluster.postgres_port
    )

    instance.query(
        "INSERT INTO postgres_database.postgresql_replica SELECT 100 + number, number from numbers(50)"
    )
    check_tables_are_synchronized(instance, "postgresql_replica")
    pg_manager.execute("UPDATE postgresql_replica SET key=key-25 WHERE key<100 ")
    check_tables_are_synchronized(instance, "postgresql_replica")


def test_clickhouse_restart(started_cluster):
    NUM_TABLES = 5
    pg_manager.create_and_fill_postgres_tables(NUM_TABLES)
    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip, port=started_cluster.postgres_port
    )
    check_several_tables_are_synchronized(instance, NUM_TABLES)

    for i in range(NUM_TABLES):
        instance.query(
            "INSERT INTO postgres_database.postgresql_replica_{} SELECT 50 + number, {} from numbers(50000)".format(
                i, i
            )
        )

    instance.restart_clickhouse()
    check_several_tables_are_synchronized(instance, NUM_TABLES)


def test_replica_identity_index(started_cluster):
    pg_manager.create_postgres_table(
        "postgresql_replica", template=postgres_table_template_3
    )
    pg_manager.execute("CREATE unique INDEX idx on postgresql_replica(key1, key2);")
    pg_manager.execute(
        "ALTER TABLE postgresql_replica REPLICA IDENTITY USING INDEX idx"
    )
    instance.query(
        "INSERT INTO postgres_database.postgresql_replica SELECT number, number, number, number from numbers(50, 10)"
    )

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip, port=started_cluster.postgres_port
    )
    instance.query(
        "INSERT INTO postgres_database.postgresql_replica SELECT number, number, number, number from numbers(100, 10)"
    )
    check_tables_are_synchronized(instance, "postgresql_replica", order_by="key1")

    pg_manager.execute("UPDATE postgresql_replica SET key1=key1-25 WHERE key1<100 ")
    pg_manager.execute("UPDATE postgresql_replica SET key2=key2-25 WHERE key2>100 ")
    pg_manager.execute(
        "UPDATE postgresql_replica SET value1=value1+100 WHERE key1<100 "
    )
    pg_manager.execute(
        "UPDATE postgresql_replica SET value2=value2+200 WHERE key2>100 "
    )
    check_tables_are_synchronized(instance, "postgresql_replica", order_by="key1")

    pg_manager.execute("DELETE FROM postgresql_replica WHERE key2<75;")
    check_tables_are_synchronized(instance, "postgresql_replica", order_by="key1")


def test_table_schema_changes(started_cluster):
    NUM_TABLES = 5

    for i in range(NUM_TABLES):
        pg_manager.create_postgres_table(
            f"postgresql_replica_{i}", template=postgres_table_template_2
        )
        instance.query(
            f"INSERT INTO postgres_database.postgresql_replica_{i} SELECT number, {i}, {i}, {i} from numbers(25)"
        )

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
    )

    for i in range(NUM_TABLES):
        instance.query(
            f"INSERT INTO postgres_database.postgresql_replica_{i} SELECT 25 + number, {i}, {i}, {i} from numbers(25)"
        )

    check_several_tables_are_synchronized(instance, NUM_TABLES)

    expected = instance.query(
        "SELECT key, value1, value3 FROM test_database.postgresql_replica_3 ORDER BY key"
    )

    altered_idx = random.randint(0, 4)
    altered_table = f"postgresql_replica_{altered_idx}"
    prev_count = int(
        instance.query(f"SELECT count() FROM test_database.{altered_table}")
    )

    pg_manager.execute(f"ALTER TABLE {altered_table} DROP COLUMN value2")
    for i in range(NUM_TABLES):
        pg_manager.execute(f"INSERT INTO postgresql_replica_{i} VALUES (50, {i}, {i})")

    assert instance.wait_for_log_line(
        f"Table postgresql_replica_{altered_idx} is skipped from replication stream"
    )
    assert prev_count == int(
        instance.query(f"SELECT count() FROM test_database.{altered_table}")
    )


def test_many_concurrent_queries(started_cluster):
    table = "test_many_conc"
    query_pool = [
        "DELETE FROM {} WHERE (value*value) % 3 = 0;",
        "UPDATE {} SET value = value - 125 WHERE key % 2 = 0;",
        "DELETE FROM {} WHERE key % 10 = 0;",
        "UPDATE {} SET value = value*5 WHERE key % 2 = 1;",
        "DELETE FROM {} WHERE value % 2 = 0;",
        "UPDATE {} SET value = value + 2000 WHERE key % 5 = 0;",
        "DELETE FROM {} WHERE value % 3 = 0;",
        "UPDATE {} SET value = value * 2 WHERE key % 3 = 0;",
        "DELETE FROM {} WHERE value % 9 = 2;",
        "UPDATE {} SET value = value + 2  WHERE key % 3 = 1;",
        "DELETE FROM {} WHERE value%5 = 0;",
    ]

    NUM_TABLES = 5

    conn = get_postgres_conn(
        ip=started_cluster.postgres_ip,
        port=started_cluster.postgres_port,
        database=True,
    )
    cursor = conn.cursor()
    pg_manager.create_and_fill_postgres_tables(
        NUM_TABLES, numbers=10000, table_name_base=table
    )

    def attack(thread_id):
        print("thread {}".format(thread_id))
        k = 10000
        for i in range(20):
            query_id = random.randrange(0, len(query_pool) - 1)
            table_id = random.randrange(0, 5)  # num tables
            random_table_name = f"{table}_{table_id}"
            table_name = f"{table}_{thread_id}"

            # random update / delete query
            cursor.execute(query_pool[query_id].format(random_table_name))
            print(
                "Executing for table {} query: {}".format(
                    random_table_name, query_pool[query_id]
                )
            )

            # allow some thread to do inserts (not to violate key constraints)
            if thread_id < 5:
                print("try insert table {}".format(thread_id))
                instance.query(
                    "INSERT INTO postgres_database.{} SELECT {}*10000*({} +  number), number from numbers(1000)".format(
                        table_name, thread_id, k
                    )
                )
                k += 1
                print("insert table {} ok".format(thread_id))

                if i == 5:
                    # also change primary key value
                    print("try update primary key {}".format(thread_id))
                    cursor.execute(
                        "UPDATE {} SET key=key%100000+100000*{} WHERE key%{}=0".format(
                            table_name, i + 1, i + 1
                        )
                    )
                    print("update primary key {} ok".format(thread_id))

    n = [10000]

    threads = []
    threads_num = 16
    for i in range(threads_num):
        threads.append(threading.Thread(target=attack, args=(i,)))

    pg_manager.create_materialized_db(
        ip=started_cluster.postgres_ip, port=started_cluster.postgres_port
    )

    for thread in threads:
        time.sleep(random.uniform(0, 1))
        thread.start()

    n[0] = 50000
    for table_id in range(NUM_TABLES):
        n[0] += 1
        table_name = f"{table}_{table_id}"
        instance.query(
            "INSERT INTO postgres_database.{} SELECT {} +  number, number from numbers(5000)".format(
                table_name, n[0]
            )
        )
        # cursor.execute("UPDATE {table}_{} SET key=key%100000+100000*{} WHERE key%{}=0".format(table_id, table_id+1, table_id+1))

    for thread in threads:
        thread.join()

    for i in range(NUM_TABLES):
        table_name = f"{table}_{i}"
        check_tables_are_synchronized(instance, table_name)
        count1 = instance.query(
            "SELECT count() FROM postgres_database.{}".format(table_name)
        )
        count2 = instance.query(
            "SELECT count() FROM (SELECT * FROM test_database.{})".format(table_name)
        )
        assert int(count1) == int(count2)
        print(count1, count2)


if __name__ == "__main__":
    cluster.start()
    input("Cluster created, press any key to destroy...")
    cluster.shutdown()
