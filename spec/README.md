# Vendored API description

`knxiot_api_openapi.yaml` and `schemas-2020/` are copied from the public repository
"KNX IoT 3rd Party API Scheme" of the KNX Association,
<https://gitlab.knx.org/public-projects/knx-iot-3rd-party-api-schema>,
commit `2eddf6debe7aef8cbb02117942b78fcb90eed945` (17 July 2024), API version 2.1.0.
They are under the MIT licence of the KNX Association, see `LICENSE.md` in this folder.

Changes made here: the files in `schemas-2020/` were re-encoded from UTF-16 to UTF-8 with
LF line ends so that they diff and load everywhere. Nothing else was edited.

They are used by the contract tests (`tests/test_api_contract.py`) only. This project
implements a subset of the API and claims no conformance.
