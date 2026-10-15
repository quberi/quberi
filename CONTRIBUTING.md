# Contributing to Quberi

Thanks for taking the time. Issues and pull requests are welcome.
For anything larger than a small fix, please open an issue first so we can
agree on the approach before you spend time on it.

## License of contributions

Quberi is licensed under [AGPL-3.0](LICENSE). By contributing, you agree that
your contribution is licensed under the same license.

There is no contributor license agreement (CLA). Contributions are accepted
under the [Developer Certificate of Origin](DCO) (DCO) instead. This means the
copyright of your contribution stays with you, and the project cannot relicense
your code under different terms without your consent.

## Sign off your commits (DCO)

Every commit in a pull request must carry a `Signed-off-by` line. It certifies
that you wrote the change or otherwise have the right to submit it under the
project's license, as set out in the [DCO](DCO).

Add the line automatically with `-s`:

```bash
git commit -s -m "Fix retrieval filter for archived spaces"
```

This appends a line like the following, using your git name and email:

```
Signed-off-by: Your Name <you@example.com>
```

Notes:

- The name and email must be real enough to identify you. They become part of the
  public history. If you do not want your personal email public, use a
  no-reply address, for example the one GitHub provides in
  Settings → Emails.
- Forgot to sign off? For the last commit: `git commit --amend -s --no-edit`.
  For several commits: `git rebase --signoff main`, then force-push your branch.

## Before you open a pull request

```bash
make check     # lint, migration drift check, API and web tests
```

- `make test-api` needs a separate `quberi_test` database; see
  [`deploy/README.md`](deploy/README.md).
- Keep changes focused. Add or update tests for behavior you change.
- Database changes need an Alembic migration (`make revision m="short slug"`),
  and `make check-drift` must pass.
- Do not commit secrets, `.env` files or real customer data.

## Security issues

Please do not report vulnerabilities in a public issue. See the Security section
of the [README](README.md).
