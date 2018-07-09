from __future__ import print_function, division, absolute_import

import os
import time
import weakref
from collections import MutableMapping
from threading import Thread

import pytest

import skein
from skein.exceptions import FileNotFoundError, FileExistsError
from skein.test.conftest import (run_application, wait_for_containers,
                                 wait_for_success, get_logs)


def test_security(tmpdir):
    path = str(tmpdir)
    s1 = skein.Security.from_new_directory(path)
    s2 = skein.Security.from_directory(path)
    assert s1 == s2

    with pytest.raises(FileExistsError):
        skein.Security.from_new_directory(path)

    # Test force=True
    with open(s1.cert_path) as fil:
        data = fil.read()

    s1 = skein.Security.from_new_directory(path, force=True)

    with open(s1.cert_path) as fil:
        data2 = fil.read()

    assert data != data2

    os.remove(s1.cert_path)
    with pytest.raises(FileNotFoundError):
        skein.Security.from_directory(path)


def test_security_auto_inits(skein_config):
    with pytest.warns(None) as rec:
        sec = skein.Security.from_default()

    assert len(rec) == 1
    assert 'Skein global security credentials not found' in str(rec[0])
    assert os.path.exists(sec.cert_path)
    assert os.path.exists(sec.key_path)

    with pytest.warns(None) as rec:
        sec2 = skein.Security.from_default()

    assert not rec
    assert sec == sec2


def pid_exists(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_client(security, kinit, tmpdir):
    logpath = str(tmpdir.join("log.txt"))

    with skein.Client(security=security, log=logpath) as client:
        # smoketests
        client.get_applications()
        repr(client)

        client2 = skein.Client(address=client.address, security=security)
        assert client2._proc is None

        # smoketests
        client2.get_applications()
        repr(client2)

    # Process was definitely closed
    assert not pid_exists(client._proc.pid)

    # no-op to call close again
    client.close()

    # Log was written
    assert os.path.exists(logpath)
    with open(logpath) as fil:
        assert len(fil.read()) > 0

    # Connection error on closed client
    with pytest.raises(skein.ConnectionError):
        client2.get_applications()

    # Connection error on connecting to missing daemon
    with pytest.raises(skein.ConnectionError):
        skein.Client(address=client.address, security=security)


def test_client_closed_when_reference_dropped(security, kinit):
    client = skein.Client(security=security, log=False)
    ref = weakref.ref(client)

    pid = client._proc.pid

    del client
    assert ref() is None
    assert not pid_exists(pid)


def test_simple_app(client):
    with run_application(client) as app:
        # smoketest repr
        repr(app)

        # Test get_specification
        a = app.get_specification()
        assert isinstance(a, skein.ApplicationSpec)
        assert 'sleeper' in a.services

        assert client.application_report(app.id).state == 'RUNNING'

        app.shutdown()

    with pytest.raises(skein.ConnectionError):
        client.connect(app.id)

    with pytest.raises(skein.ConnectionError):
        client.connect(app.id, wait=False)

    with pytest.raises(skein.ConnectionError):
        app.get_specification()

    running_apps = client.get_applications()
    assert app.id not in {a.id for a in running_apps}

    finished_apps = client.get_applications(states=['finished'])
    assert app.id in {a.id for a in finished_apps}


def test_key_value(client):
    with run_application(client) as app:
        assert isinstance(app.kv, MutableMapping)
        assert app.kv is app.kv

        assert dict(app.kv) == {}

        app.kv['foo'] = 'bar'
        assert app.kv['foo'] == 'bar'

        assert dict(app.kv) == {'foo': 'bar'}
        assert app.kv.to_dict() == {'foo': 'bar'}
        assert len(app.kv) == 1

        del app.kv['foo']
        assert app.kv.to_dict() == {}
        assert len(app.kv) == 0

        with pytest.raises(KeyError):
            app.kv['fizz']

        with pytest.raises(TypeError):
            app.kv[1] = 'foo'

        with pytest.raises(TypeError):
            app.kv['foo'] = 1

        def set_foo():
            time.sleep(0.5)
            app.kv['foo'] = 'baz'

        setter = Thread(target=set_foo)
        setter.daemon = True
        setter.start()

        val = app.kv.wait('foo')
        assert val == 'baz'

        # Get immediately for set keys
        val2 = app.kv.wait('foo')
        assert val2 == 'baz'

        app.shutdown()


def test_dynamic_containers(client):
    with run_application(client) as app:
        initial = wait_for_containers(app, 1, states=['RUNNING'])
        assert initial[0].state == 'RUNNING'
        assert initial[0].service_name == 'sleeper'

        # Scale sleepers up to 3 containers
        new = app.scale('sleeper', 3)
        assert len(new) == 2
        for c in new:
            assert c.state == 'REQUESTED'
        wait_for_containers(app, 3, services=['sleeper'], states=['RUNNING'])

        # Scale down to 1 container
        stopped = app.scale('sleeper', 1)
        assert len(stopped) == 2
        # Stopped oldest 2 instances
        assert stopped[0].instance == 0
        assert stopped[1].instance == 1

        # Scale up to 2 containers
        new = app.scale('sleeper', 2)
        # Calling twice is no-op
        new2 = app.scale('sleeper', 2)
        assert len(new2) == 0
        assert new[0].instance == 3
        current = wait_for_containers(app, 2, services=['sleeper'],
                                      states=['RUNNING'])
        assert current[0].instance == 2
        assert current[1].instance == 3

        # Manually kill instance 3
        app.kill_container('sleeper_3')
        current = app.get_containers()
        assert len(current) == 1
        assert current[0].instance == 2

        # Fine to kill already killed container
        app.kill_container('sleeper_1')

        # All killed containers
        killed = app.get_containers(states=['killed'])
        assert len(killed) == 3
        assert [c.instance for c in killed] == [0, 1, 3]

        # Can't scale non-existant service
        with pytest.raises(ValueError):
            app.scale('foobar', 2)

        # Can't scale negative
        with pytest.raises(ValueError):
            app.scale('sleeper', -5)

        # Can't kill non-existant container
        with pytest.raises(ValueError):
            app.kill_container('foobar_1')

        with pytest.raises(ValueError):
            app.kill_container('sleeper_500')

        # Invalid container id
        with pytest.raises(ValueError):
            app.kill_container('fooooooo')

        # Can't get containers for non-existant service
        with pytest.raises(ValueError):
            app.get_containers(services=['sleeper', 'missing'])

        app.shutdown()


def test_container_permissions(client, has_kerberos_enabled):
    commands = ['echo "USER_ENV=[$USER]"',
                'echo "LOGIN_ID=[$(whoami)]"',
                'env',
                'hdfs dfs -touchz /user/testuser/test_container_permissions']
    service = skein.Service(resources=skein.Resources(memory=128, vcores=1),
                            commands=commands)
    spec = skein.ApplicationSpec(name="test_container_permissions",
                                 queue="default",
                                 services={'service': service})

    with run_application(client, spec=spec) as app:
        wait_for_success(client, app.id)

    logs = get_logs(app.id)
    assert "USER_ENV=[testuser]" in logs
    if has_kerberos_enabled:
        assert "LOGIN_ID=[testuser]" in logs
        assert "HADOOP_USER_NAME" not in logs
    else:
        assert "LOGIN_ID=[yarn]" in logs
        assert "HADOOP_USER_NAME" in logs