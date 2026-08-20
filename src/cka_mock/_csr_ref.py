"""Deterministic reference key + CSR for the ``csr`` archetype.

Generated once with ``openssl genrsa`` + ``openssl req -new -key ... -subj /CN=cka-mock-ref``.
The Kubernetes CSR signer signs any approved request regardless of the key, so the
reference can use a fixed pair while the candidate uses their own key. Content is
never graded — only the CSR's approval/signing and the resulting Secret's PEM
structure are checked.
"""
from __future__ import annotations

REF_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC97yH8YWmvfRzz
JM67YSs5Jnql0gmi0E79Lz37SxspYkMY0VYYMpFnuOy+62L22/+RzLuNz9NSRhjK
K9LYBQNc9zqU6R+7B177Lkg8Zwo/NlWnkz/5JXDcImtX1cd+ZhkoWhs+aJbr6chH
Z9Gh9D0q8F77RT82oKyGp4IigiuS8rasa5lXKRp10VGsoofLPYYrJnFfNWJxRGJv
oPL5pd+j/Ehqz7j+NjC9lCRT9B/8D0lYxmuFGg7mccxzkKNOT4yDvrnPfMEIbV82
0EDVW9nRk/vdYpTfoqdZsg1nEfJgLr2gEGFHvePx1HGMWtVsP/NqXfO/Pa3aD+Fl
2xb+DJLhAgMBAAECggEAW/V0ZH/7jfJK+nQHPdTxevWk6zUS4kft/oqJfWojR/mh
19m+z+9n1CrGQTexTik9B2fzJNmLDKoQpY3rQxjGRJQUwnVOnhyXAZEqe0g+P1W8
yeoyzpuwlXx6sWe5c3rWUmGyEBjOfYFOrTfYWQvqM6h2rDyeOwI3FejzSmbw1u+j
Y8asBuAMNxZvNQqWhRtQgqveWyC3NPDtb3vW+fDCWDvnJKOnR8/jREWfs657UoCs
5WqfZYX6RtE1lwaMzIyGeSuqDfdHkNqjG4vUVn0OxLIzJCYARj4bCq+BO6jvXeDM
fuo5j8lrogw0EHtm8B2dM5NP2zHN8GJbOf9fENVSSwKBgQDw2yzksTYZCjPD6SGP
KpKsJN88Nu9HXQv2pB7MKNBHhOdNX54FbpD8meLDFlxofdaAqsNPqYXmeYE/2IIl
wzXpU83jdOy/Vep3ibkdwxbsX2DN8v/mo2NUMIsOD9AUMiNaUznnldRFzNQMoDUn
bnu85LRmIWh0hSjtlw8FuidgJwKBgQDJ4FC8LeXIDVks77jJ7stw8ZOnvxRPzkhF
hG11KU0w8MQSfSS4gc2r/t0UgP6tBFbfxwv9NeBQGOh2PUxAgB7/mJCWw+MKq/yd
Q5QJrtlJdHee/jxKNwxtCEVi0Tsv+m63qjn+YmYoxbVTOlqmgQssC1ste5yFdpuP
gAyVD8HRtwKBgQDhAlEBYRCB+gb8KgqA/ivCCDxIu6V0TNfFVfOzlRlnByEzJnWw
62clpPm0eWpg79Z5o44NGPkPpEl8BN8pOiJeVT1OFkiqQzBk9lPrGvuKXVxJao9o
vxEncKYnv9wLUf+2/XNmB5Ikl0MufGjL6qpMgHiLxQAZguEGfQ26svrgFwKBgCPc
fzsOhDUneeHrq83jZ6xmna482WTb5ibDQZaAgb/h5VLLfExycpDNM4TC7XravHwA
JfcTLQfNhO4MMZF7sQYCmhzOK3Tn3HYrXQ7jSqIr2FwcGaWtZb2wrHLDbFo3iJeD
281+uVsg0/c29IeDgPKQCeBohwOGcFfrjVClfe69AoGBALuo/AqjTk4AidXotHtE
YctQzES/zJFMCHIqYF+1khgBMX9/JERwj8ga0JdDcXD6kTAGh+SwIVZkUKvKQ2EJ
eppNVo0VUvrGZnygjEi/d2UkTSJcsy915vhALnF1x/AXyRzrxl4ON19fpHUi+ul/
X6sOMKAwRHM0aSGjFCDlqFqd
-----END PRIVATE KEY-----"""

# base64-encoded DER CertificateSigningRequest matching REF_PRIVATE_KEY.
REF_CSR_B64 = (
    "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURSBSRVFVRVNULS0tLS0KTUlJQ1hEQ0NBVVFDQVFB"
    "d0Z6RVZNQk1HQTFVRUF3d01ZMnRoTFcxdlkyc3RjbVZtTUlJQklqQU5CZ2txaGtpRwo5"
    "dzBCQVFFRkFBT0NBUThBTUlJQkNnS0NBUUVBdmU4aC9HRnByMzBjOHlUT3UyRXJPU1o2"
    "cGRJSm90Qk8vUzg5Ciswc2JLV0pER05GV0dES1JaN2pzdnV0aTl0di9rY3k3amMvVFVr"
    "WVl5aXZTMkFVRFhQYzZsT2tmdXdkZSt5NUkKUEdjS1B6WlZwNU0vK1NWdzNDSnJWOVhI"
    "Zm1ZWktGb2JQbWlXNituSVIyZlJvZlE5S3ZCZSswVS9OcUNzaHFlQwpJb0lya3ZLMnJH"
    "dVpWeWthZGRGUnJLS0h5ejJHS3laeFh6VmljVVJpYjZEeSthWGZvL3hJYXMrNC9qWXd2"
    "WlFrClUvUWYvQTlKV01acmhSb081bkhNYzVDalRrK01nNzY1ejN6QkNHMWZOdEJBMVZ2"
    "WjBaUDczV0tVMzZLbldiSU4KWnhIeVlDNjlvQkJoUjczajhkUnhqRnJWYkQvemFsM3p2"
    "ejJ0MmcvaFpkc1cvZ3lTNFFJREFRQUJvQUF3RFFZSgpLb1pJaHZjTkFRRUxCUUFEZ2dF"
    "QkFEQWw0OFgvTlVaN3pKdjFBa3JEcVBWTU82MG5wZVlDUHBEbVZYUDdkVzAxCjcySUg1"
    "V0ZBYzE2UGFycFFJVkIzbmk1aXVONTZEM0xvSWNnWElWZXdISG9weXpSNTloaEdGOXhZ"
    "TnNjeFBQZVEKbWZlRHVvSG9ZTjZJeGlGQnhJa1NMbCtPeTlnTjg1Mzh4WkxsaG1STU1z"
    "NjRoTnlNSEpXNkswVWl0VVZPZWxuRwpzVzRQbmg2TTF6U0xqMWhHTWF1eVpDdU1JTlhy"
    "RzJZdFVDRGRuMDNrQzB1UVpkcHhuVFhyUzFXdXVqR0VWVjNZCm9tcFJRS0dYUzRvTjVs"
    "aTMxMGVENHpsYjFvVVg2bU1lbEFsMStNRGFZSnB2OXNnTXFCYitMM2wzcXVVRm5uZlkK"
    "WEpxMmo0U2pZV25UOVZhWlJ0T0U5VnYvbkVQZVpVQUEyN0RqN2gyeTVUZz0KLS0tLS1F"
    "TkQgQ0VSVElGSUNBVEUgUkVRVUVTVC0tLS0tCg=="
)

# base64 (PEM) prefixes for structural Secret checks. `kubectl create secret tls`
# base64-encodes the PEM file verbatim, so the stored value starts with the base64
# of the header line.
CERT_PEM_HEADER_B64 = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0t"  # base64("-----BEGIN CERTIFICATE-----")
PRIV_KEY_PEM_HEADER_B64 = "LS0tLS1CRUdJTiBQUklWQVRFIEtFWS0tLS0t"  # base64("-----BEGIN PRIVATE KEY-----")