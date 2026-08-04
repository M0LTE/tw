# EIR request to Thames Water

Ready to send. Tracked in [#8](https://github.com/M0LTE/tw/issues/8).

Water and sewerage undertakers are public authorities under the Environmental
Information Regulations 2004 for their relevant functions, following *Fish Legal v
Information Commissioner*. The response deadline is **20 working days** from receipt.

**Route:** Thames Water's published contact for information rights requests. Confirm the
current address on their website before sending rather than relying on this file — send to
their data protection / information rights team, not the general customer contact form,
and say "Environmental Information Regulations 2004" in the subject so it is routed
correctly.

**Before sending:** items 1–4 are deliberately narrow. Do not add anything already
published — asking for material that is on their own map weakens the rest of the request.
In particular, the Completed/Canceled split is already public in
`CleanWaterClosedWorkOrder` and `WasteWaterClosedWorkOrder`, so item 1 asks only for what
sits behind it.

---

## Suggested text

> **Subject: Request for environmental information — Environmental Information Regulations 2004**
>
> Dear Thames Water,
>
> I am requesting the following information under the Environmental Information Regulations
> 2004. I understand Thames Water is a public authority for the purposes of those
> Regulations in respect of its water and sewerage functions, following *Fish Legal v
> Information Commissioner* [2015] UKUT 52 (AAC).
>
> This request concerns the operational data published through your "report a problem" map
> at https://www.thameswater.co.uk/help/report-a-problem#/view-problems-map, and the ArcGIS
> feature layers behind it.
>
> **1. Work order closure reasons.**
> Your published `CleanWaterClosedWorkOrder` and `WasteWaterClosedWorkOrder` layers give a
> `WorkOrderStatus` of either "Completed" or "Canceled". Please provide:
>
> a) the full list of closure reason codes or dispositions recorded internally against a
> work order when it is closed, with the meaning of each; and
>
> b) the criteria that determine whether a work order is closed as "Canceled" rather than
> "Completed", including whether a cancellation can follow a completed repair.
>
> **2. Work orders withheld from the public map.**
> Records in the work order layers carry a `ShowOnMapIndicator` field. For each month over
> the last 24 months, please provide the number of open work orders with
> `ShowOnMapIndicator` set to a value other than "Yes", broken down by `JourneyType`,
> together with the criteria that determine whether a work order is shown publicly.
>
> **3. Retention of the published layers.**
> For each of `CleanWaterOpenWorkOrder`, `WasteWaterOpenWorkOrder`,
> `CleanWaterClosedWorkOrder`, `WasteWaterClosedWorkOrder` and the pending-pins layer behind
> the "Leak" markers, please state:
>
> a) how long a record remains in the layer before it is removed;
>
> b) what determines removal (elapsed time, status change, or otherwise); and
>
> c) whether removed records are retained elsewhere and remain available.
>
> **4. Street works permit lead times for repairs.**
> In your response to Ofwat's consultation on the Guaranteed Standards Scheme (September
> 2025), you stated that a local authority road closure permit "can be 12 weeks", in the
> context of installing water meters under Regulation 17IB. Please provide the equivalent
> figures for **repair** work: the mean and median elapsed time between a work order
> reaching a status of "Repair Planning" and the corresponding street works permit being
> granted, for each of the last 24 months.
>
> I would prefer the response in a machine-readable format (CSV or JSON) where the
> information is held in that form.
>
> If any part of this request is refused, please state the exception relied on and your
> reasoning on the public interest test, and confirm the internal review process.
>
> If you consider any part of this request unclear or unduly burdensome, please contact me
> to discuss narrowing it rather than refusing it outright, as required by Regulation 9.
>
> Yours faithfully,

---

## What to do with the response

Record the outcome on #8 either way. A refusal is publishable in itself: we can show exactly
what is withheld and why it matters to the published figures, which is a stronger position
than not having asked.

If items 1 or 3 are answered, they change the site directly — item 1 lets us describe
cancellations properly, and item 3 tells us how much we permanently lose by polling twice a
day rather than hourly, which is the open question on #16.
