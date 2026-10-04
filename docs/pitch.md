# The sixty-second explanation

Treat this as a deliverable with the same weight as the code. The project's
value is in the conversation it starts, and that only works if it can be said
plainly out loud.

Roughly 165 words, which is about 60 seconds at a normal speaking pace. Written
for speaking, so the sentences are shorter than written prose.

---

When you call a payment API and it times out, you don't know whether the charge
went through. Retry, and you might charge twice. Stop, and the customer never
paid. The error tells you nothing either way.

That's annoying in ordinary code. With an AI agent it's worse, because the agent
picks what to do next, and the process running it can die halfway through.

So I built a lab to measure it. A payment service that commits the charge and
returns an error anyway. A separate ledger the agent can't see, recording what
really happened. And a switch that kills the worker at an exact instant: right
after the payment lands, before my own side writes it down.

Then I ran the same job three ways: the model deciding for itself, a plain retry
with one fixed ticket number, and a durable version that writes its intent down
before acting, with a background worker that finishes abandoned jobs.

The part I didn't expect was ___.

---

## The last line

**Before the frozen run exists**, do not use the blank. End with:

> I'm partway through the measurement. What I'm watching for is whether the
> durable version actually beats a plain retry, because on the simple failures
> I don't think it will.

Predicting a tie before measuring is what makes a listener trust the rest.

**After the frozen run**, fill the blank with whichever is true:

- how often the model double-charged when it had no way to check payment status
- that the plain retry matched the durable version on simple failures and only
  fell behind once the process was killed mid-payment

Never fill it with a result that has not been measured and scored against the
independent ledger.

## Tense

Say "I'm building" until it exists. "I built" only after.

## One-sentence version

A system that can be killed mid-payment and still charge exactly once, with
proof.
