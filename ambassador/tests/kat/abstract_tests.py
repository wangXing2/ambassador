import sys

import base64
import os
import pytest
import shutil
import subprocess
import yaml

from typing import Any, ClassVar, Optional, Sequence
from typing import cast as typecast

from kat.harness import abstract_test, sanitize, Name, Node, Test, Query
from kat import manifests

AMBASSADOR_LOCAL = """
---
apiVersion: v1
kind: Service
metadata:
  name: {self.path.k8s}
spec:
  type: NodePort
  ports:
  - name: http
    protocol: TCP
    port: 80
    targetPort: 80
  - name: https
    protocol: TCP
    port: 443
    targetPort: 443
  selector:
    service: {self.path.k8s}
---
apiVersion: v1
kind: Service
metadata:
  labels:
    service: {self.path.k8s}-admin
  name: {self.path.k8s}-admin
spec:
  type: NodePort
  ports:
  - name: {self.path.k8s}-admin
    port: 8877
    targetPort: 8877
  selector:
    service: {self.path.k8s}
---
apiVersion: rbac.authorization.k8s.io/v1beta1
kind: ClusterRole
metadata:
  name: {self.path.k8s}
rules:
- apiGroups: [""]
  resources:
  - services
  verbs: ["get", "list", "watch"]
- apiGroups: [""]
  resources:
  - configmaps
  verbs: ["create", "update", "patch", "get", "list", "watch"]
- apiGroups: [""]
  resources:
  - secrets
  verbs: ["get", "list", "watch"]
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {self.path.k8s}
---
apiVersion: rbac.authorization.k8s.io/v1beta1
kind: ClusterRoleBinding
metadata:
  name: {self.path.k8s}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {self.path.k8s}
subjects:
- kind: ServiceAccount
  name: {self.path.k8s}
  namespace: default
---
apiVersion: v1
kind: Secret
metadata:
  name: {self.path.k8s}
  annotations:
    kubernetes.io/service-account.name: {self.path.k8s}
type: kubernetes.io/service-account-token
"""


def run(*args, **kwargs):
    for arg in "stdout", "stderr":
        if arg not in kwargs:
            kwargs[arg] = subprocess.PIPE
    return subprocess.run(args, **kwargs)


DEV = os.environ.get("AMBASSADOR_DEV", "0").lower() in ("1", "yes", "true")


@abstract_test
class AmbassadorTest(Test):

    """
    AmbassadorTest is a top level ambassador test.
    """

    OFFSET: ClassVar[int] = 0
    IMAGE_BUILT: ClassVar[bool] = False

    _index: Optional[int] = None
    _ambassador_id: Optional[str] = None
    env = []
    name: Name
    path: Name

    def manifests(self) -> str:
        if DEV:
            return self.format(AMBASSADOR_LOCAL)
        else:
            return self.format(manifests.AMBASSADOR, image=os.environ["AMBASSADOR_DOCKER_IMAGE"], envs="")

    # Will tear this out of the harness shortly
    @property
    def ambassador_id(self) -> str:
        if self._ambassador_id is None:
            return self.name.k8s
        else:
            return typecast(str, self._ambassador_id)

    @ambassador_id.setter
    def ambassador_id(self, val: str) -> None:
        self._ambassador_id = val

    @property
    def index(self) -> int:
        if self._index is None:
            # lock here?
            self._index = AmbassadorTest.OFFSET
            AmbassadorTest.OFFSET += 1

        return typecast(int, self._index)

    def post_manifest(self):
        if not DEV:
            return

        run("docker", "kill", self.path.k8s)
        run("docker", "rm", self.path.k8s)

        image = os.environ["AMBASSADOR_DOCKER_IMAGE"]

        if not AmbassadorTest.IMAGE_BUILT:
            AmbassadorTest.IMAGE_BUILT = True
            context = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
            print("Starting docker build...", end="")
            sys.stdout.flush()
            result = run("docker", "build", context, "-t", image)
            try:
                result.check_returncode()
                print("done.")
            except Exception as e:
                print((result.stdout + b"\n" + result.stderr).decode("utf8"))
                pytest.exit("container failed to build")

        fname = "/tmp/k8s-%s.yaml" % self.path.k8s
        if os.path.exists(fname):
            with open(fname) as fd:
                content = fd.read()
        else:
            result = run("kubectl", "get", "-o", "yaml", "secret", self.path.k8s)
            result.check_returncode()
            with open(fname, "wb") as fd:
                fd.write(result.stdout)
            content = result.stdout
        secret = yaml.load(content)
        data = secret['data']
        # secret_dir = tempfile.mkdtemp(prefix=self.path.k8s, suffix="secret")
        secret_dir = "/tmp/%s-ambassadormixin-%s" % (self.path.k8s, 'secret')

        shutil.rmtree(secret_dir, ignore_errors=True)
        os.mkdir(secret_dir, 0o777)

        for k, v in data.items():
            with open(os.path.join(secret_dir, k), "wb") as f:
                f.write(base64.decodebytes(bytes(v, "utf8")))
        print("Launching %s container." % self.path.k8s)
        command = ["docker", "run", "-d", "--name", self.path.k8s]

        envs = ["KUBERNETES_SERVICE_HOST=kubernetes", "KUBERNETES_SERVICE_PORT=443",
                "AMBASSADOR_ID=%s" % self.ambassador_id]
        envs.extend(self.env)
        [command.extend(["-e", env]) for env in envs]

        ports = ["%s:8877" % (8877 + self.index), "%s:80" % (8080 + self.index), "%s:443" % (8443 + self.index)]
        [command.extend(["-p", port]) for port in ports]

        volumes = ["%s:/var/run/secrets/kubernetes.io/serviceaccount" % secret_dir]
        [command.extend(["-v", volume]) for volume in volumes]

        command.append(image)

        result = run(*command)
        result.check_returncode()

    def queries(self):
        if DEV:
            result = run("docker", "ps", "-qf", "name=%s" % self.path.k8s)
            result.check_returncode()
            if not result.stdout.strip():
                result = run("docker", "logs", self.path.k8s, stderr=subprocess.STDOUT)
                result.check_returncode()
                print(result.stdout.decode("utf8"), end="")
                pytest.exit("container failed to start")
        return ()

    def scheme(self) -> str:
        return "http"

    def url(self, prefix, scheme=None) -> str:
        if scheme is None:
            scheme = self.scheme()
        if DEV:
            port = 8443 if scheme == 'https' else 8080
            return "%s://%s/%s" % (scheme, "localhost:%s" % (port + self.index), prefix)
        else:
            return "%s://%s/%s" % (scheme, self.path.k8s, prefix)

    def requirements(self):
        yield ("url", Query(self.url("ambassador/v0/check_ready")))
        yield ("url", Query(self.url("ambassador/v0/check_alive")))


@abstract_test
class ServiceType(Node):

    path: Name

    def config(self):
        yield from ()

    def manifests(self):
        return self.format(manifests.BACKEND)

    def requirements(self):
        yield ("url", Query("http://%s" % self.path.k8s))
        yield ("url", Query("https://%s" % self.path.k8s))


class HTTP(ServiceType):
    pass


class GRPC(ServiceType):
    pass


@abstract_test
class MappingTest(Test):

    target: ServiceType
    options: Sequence['OptionTest']
    parent: AmbassadorTest

    def init(self, target: ServiceType, options=()) -> None:
        self.target = target
        self.options = list(options)


@abstract_test
class OptionTest(Test):

    VALUES: ClassVar[Any] = None
    value: Any
    parent: Test

    @classmethod
    def variants(cls):
        if cls.VALUES is None:
            yield cls()
        else:
            for val in cls.VALUES:
                yield cls(val, name=sanitize(val))

    def init(self, value=None):
        self.value = value
