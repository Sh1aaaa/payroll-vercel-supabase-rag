

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


        rows = (

            query

            .order(
                "created_at",
                desc=True
            )

            .execute()

            .data

            or []

        )


    except Exception as e:

        print(
            "COMPLAINT LIST ERROR:",
            repr(e)
        )

        rows = []


    return render_template(

        "complaints.html",

        complaints=rows

    )


# =========================================================
# RUN LOCALLY
# =========================================================

if __name__ == "__main__":

    app.run(

        host="0.0.0.0",

        port=int(
            os.getenv(
                "PORT",
                5000
            )
        ),

        debug=True

    )
