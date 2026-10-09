# Third-party notices

## RetryLedger

Source: https://github.com/Zhuoxi2000/RetryLedger
Revision: 58fdbc0d45dffbddccc88bceea08a4974f6573a3
File: code/ei_retryledger.py (SHA-256 b1961cd8daac7606f252dce7dc78e5e5a61adaacce64ab770eadf17c6cd2a077)

Used as follows:

- `tests/reference/retryledger_original.py` is a verbatim copy, used only as the
  reference oracle in compatibility-profile parity tests.
- The compatibility profile in `src/lab/simulator/service.py` and
  `src/lab/protocol/render.py` reproduces its task constants, tool shapes, response
  strings and legacy score definition.
- The controlled profile is this project's modification and is not an exact replication.

Licence, copied from the upstream repository at the revision above:

```text
MIT License

Copyright (c) 2026 The RetryLedger authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
