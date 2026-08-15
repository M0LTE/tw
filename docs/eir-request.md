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

**Before sending:** items 1–6 are deliberately narrow. Do not add anything already
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
> **5. The meaning of "Repair Complete", and the line item model.**
> Records in the work order layers carry a `WorkOrderStatus` of "Repair Complete", together
> with `OpenWorkOrderLineItemCount` and `ClosedWorkOrderLineItemCount` fields, a
> `WORepairCompleteDateTime`, and a `RemainOnMapInHrs` of 72. Across your published closed
> work order layers, 79.8% of records marked "Repair Complete" carry a non-zero
> `OpenWorkOrderLineItemCount`, against 2.8% of those marked "Completed". Please provide:
>
> a) the definition of a line item, and what an open line item on a work order signifies;
>
> b) the criteria under which a work order's status is set to "Repair Complete", and whether
> that status is intended to indicate that the physical repair is finished; and
>
> c) what `WORepairCompleteDateTime` records, given that it is revised forward on the
> majority of work orders that reach this status — of 1,093 such records we observed, 751 had
> the date revised afterwards, across 2,001 revisions, every one of which moved the date
> later and none of which moved it earlier; and
>
> d) whether `RemainOnMapInHrs` is counted from `WORepairCompleteDateTime`, and therefore
> whether revising that date extends the period a work order remains visible on the public
> map. We observe records displaying "Repair Complete" for up to 10.4 days against a
> `RemainOnMapInHrs` of 72.
>
> **6. Interim reinstatements not yet made permanent.**
> The Specification for the Reinstatement of Openings in Highways (Fourth Edition, May 2020)
> provides at S1.1.4 that "an interim reinstatement must normally be made permanent within
> six months". Street Manager records the reinstatement state of each excavation and derives
> an interim period end date six months from the reinstatement date, but reinstatement
> records are not included in its open data. For each month over the last 24 months, please
> provide the number of Thames Water excavations recorded in Street Manager as being in an
> interim reinstatement state, and of those, the number where the interim period end date
> had passed without a permanent reinstatement being registered.
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

Items 1, 3 and 5 change the site directly if answered. Item 1 lets us describe cancellations
properly. Item 3 tells us how much a record can pass through the feed unseen even at hourly
collection. Item 5 is the one to press hardest: the site currently publishes a finding about
"Repair Complete" (#32, #33) resting on the contrast between two statuses and on the
direction the completion date moves, and states plainly that we cannot see what a line item
is. A definition would either confirm the reading or correct it, and we have committed in
public to saying so either way.

Item 6 is the only one whose subject is held by a third party as well — DfT hold the same
reinstatement records in Street Manager. If Thames Water refuse it as not held or as
environmental information held by another authority, the same request goes to DfT, who
publish permits but not reinstatements.
