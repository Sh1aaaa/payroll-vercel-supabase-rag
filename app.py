        )

        flash(
            f"Could not approve payroll: {str(e)}",
            "error"
        )


    return redirect(
        url_for(
            "payroll_detail",
            run_id=run_id
        )
    )


# =========================================================
# COMPLAINTS / RAG
# =========================================================

@app.route(
    "/complaints",
    methods=["GET", "POST"]
)
@login_required
def complaints():

    db = admin_client()


    if request.method == "POST":

        complaint_text = request.form.get(
            "complaint",
            ""
        ).strip()


        if not complaint_text:

            flash(
                "Enter your complaint.",
                "error"
            )

            return redirect(
                url_for("complaints")
            )


        try:

            # -------------------------------------------------
            # RAG ASSESSMENT
            # -------------------------------------------------

            if assess_complaint:

                try:

                    assessment = assess_complaint(
                        complaint_text
                    )

                except Exception as rag_error:

                    print(
                        "RAG ASSESSMENT ERROR:",
                        repr(rag_error)
                    )

                    assessment = (
                        "RAG assessment is "
                        "temporarily unavailable."
                    )

            else:

                assessment = (
                    "RAG service is unavailable."
                )


            # -------------------------------------------------
            # SAVE COMPLAINT
            # -------------------------------------------------

            db.table("complaints").insert({

                "employee_id":
                    session.get(
                        "employee_id"
                    ),

                "complaint":
                    complaint_text,

                "assessment":
                    assessment,

                "status":
                    "assessed"

            }).execute()


            flash(
                "Complaint submitted.",
                "success"
            )


            return redirect(
                url_for("complaints")
            )


        except Exception as e:

            print(
                "COMPLAINT ERROR:",
                repr(e)
            )

            flash(
                f"Complaint submission failed: {str(e)}",
                "error"
            )


    # =====================================================
    # LOAD COMPLAINTS
    # =====================================================

    profile = current_profile()


    try:

        query = (
            db.table("complaints")
            .select("*")
        )


        if (
            not profile
            or profile.get("role")
            not in (
                "super_admin",
                "hr"
            )
        ):

            query = query.eq(
                "user_id",
                session.get("user_id")
            )

