"""
:class:`IRGenerator` transforms typed AST into ARTIQ intermediate
representation. ARTIQ IR is designed to be low-level enough that
its operations are elementary--contain no internal branching--
but without too much detail, such as exposing the reference/value
semantics explicitly.
"""

from collections import OrderedDict
from pythonparser import algorithm, diagnostic, ast
from .. import types, builtins, ir

def _readable_name(insn):
    if isinstance(insn, ir.Constant):
        return str(insn.value)
    else:
        return insn.name

def _extract_loc(node):
    if "keyword_loc" in node._locs:
        return node.keyword_loc
    else:
        return node.loc

# We put some effort in keeping generated IR readable,
# i.e. with a more or less linear correspondence to the source.
# This is why basic blocks sometimes seem to be produced in an odd order.
class IRGenerator(algorithm.Visitor):
    """
    :class:`IRGenerator` contains a lot of internal state,
    which is effectively maintained in a stack--with push/pop
    pairs around any state updates. It is comprised of following:

    :ivar current_loc: (:class:`pythonparser.source.Range`)
        source range of the node being currently visited
    :ivar current_function: (:class:`ir.Function` or None)
        module, def or lambda currently being translated
    :ivar current_globals: (set of string)
        set of variables that will be resolved in global scope
    :ivar current_block: (:class:`ir.BasicBlock`)
        basic block to which any new instruction will be appended
    :ivar current_env: (:class:`ir.Environment`)
        the chained function environment, containing variables that
        can become upvalues
    :ivar current_private_env: (:class:`ir.Environment`)
        the private function environment, containing internal state
    :ivar current_assign: (:class:`ir.Value` or None)
        the right-hand side of current assignment statement, or
        a component of a composite right-hand side when visiting
        a composite left-hand side, such as, in ``x, y = z``,
        the 2nd tuple element when visting ``y``
    :ivar break_target: (:class:`ir.BasicBlock` or None)
        the basic block to which ``break`` will transfer control
    :ivar continue_target: (:class:`ir.BasicBlock` or None)
        the basic block to which ``continue`` will transfer control
    :ivar return_target: (:class:`ir.BasicBlock` or None)
        the basic block to which ``return`` will transfer control
    :ivar unwind_target: (:class:`ir.BasicBlock` or None)
        the basic block to which unwinding will transfer control
    """

    _size_type = builtins.TInt(types.TValue(32))

    def __init__(self, module_name, engine):
        self.engine = engine
        self.functions = []
        self.name = [module_name]
        self.current_loc = None
        self.current_function = None
        self.current_globals = set()
        self.current_block = None
        self.current_env = None
        self.current_private_env = None
        self.current_assign = None
        self.break_target = None
        self.continue_target = None
        self.return_target = None
        self.unwind_target = None

    def add_block(self, name=""):
        block = ir.BasicBlock([], name)
        self.current_function.add(block)
        return block

    def append(self, insn, block=None, loc=None):
        if loc is None:
            loc = self.current_loc
        if block is None:
            block = self.current_block

        if insn.loc is None:
            insn.loc = loc
        return block.append(insn)

    def terminate(self, insn):
        if not self.current_block.is_terminated():
            self.append(insn)
        else:
            insn.drop_references()

    # Visitors

    def visit(self, obj):
        if isinstance(obj, list):
            for elt in obj:
                self.visit(elt)
                if self.current_block.is_terminated():
                    break
        elif isinstance(obj, ast.AST):
            try:
                old_loc, self.current_loc = self.current_loc, _extract_loc(obj)
                return self._visit_one(obj)
            finally:
                self.current_loc = old_loc

    # Module visitor

    def visit_ModuleT(self, node):
        # Treat start of module as synthesized
        self.current_loc = None

        try:
            typ = types.TFunction(OrderedDict(), OrderedDict(), builtins.TNone())
            func = ir.Function(typ, ".".join(self.name + ['__modinit__']), [])
            self.functions.append(func)
            old_func, self.current_function = self.current_function, func

            entry = self.add_block("entry")
            old_block, self.current_block = self.current_block, entry

            env = self.append(ir.Alloc([], ir.TEnvironment(node.typing_env), name="env"))
            old_env, self.current_env = self.current_env, env

            priv_env = self.append(ir.Alloc([], ir.TEnvironment({ ".return": typ.ret }),
                                            name="privenv"))
            old_priv_env, self.current_private_env = self.current_private_env, priv_env

            self.generic_visit(node)
            self.terminate(ir.Return(ir.Constant(None, builtins.TNone())))

            return func
        finally:
            self.current_function = old_func
            self.current_block = old_block
            self.current_env = old_env
            self.current_private_env = old_priv_env

    # Statement visitors

    def visit_function(self, node, is_lambda):
        if is_lambda:
            name = "lambda.{}.{}".format(node.loc.line(), node.loc.column())
            typ = node.type.find()
        else:
            name = node.name
            typ = node.signature_type.find()

        try:
            defaults = []
            for arg_name, default_node in zip(typ.optargs, node.args.defaults):
                default = self.visit(default_node)
                env_default_name = \
                    self.current_env.type.add("default." + arg_name, default.type)
                self.append(ir.SetLocal(self.current_env, env_default_name, default))
                defaults.append(env_default_name)

            old_name, self.name = self.name, self.name + [name]

            env_arg  = ir.EnvironmentArgument(self.current_env.type, "outerenv")

            args = []
            for arg_name in typ.args:
                args.append(ir.Argument(typ.args[arg_name], "arg." + arg_name))

            optargs = []
            for arg_name in typ.optargs:
                optargs.append(ir.Argument(ir.TSSAOption(typ.optargs[arg_name]), "arg." + arg_name))

            func = ir.Function(typ, ".".join(self.name), [env_arg] + args + optargs)
            self.functions.append(func)
            old_func, self.current_function = self.current_function, func

            entry = self.add_block()
            old_block, self.current_block = self.current_block, entry

            old_globals, self.current_globals = self.current_globals, node.globals_in_scope

            env_without_globals = \
                {var: node.typing_env[var]
                 for var in node.typing_env
                  if var not in node.globals_in_scope}
            env_type = ir.TEnvironment(env_without_globals, self.current_env.type)
            env = self.append(ir.Alloc([], env_type, name="env"))
            old_env, self.current_env = self.current_env, env

            if not is_lambda:
                priv_env = self.append(ir.Alloc([], ir.TEnvironment({ ".return": typ.ret }),
                                                name="privenv"))
                old_priv_env, self.current_private_env = self.current_private_env, priv_env

            self.append(ir.SetLocal(env, ".outer", env_arg))
            for index, arg_name in enumerate(typ.args):
                self.append(ir.SetLocal(env, arg_name, args[index]))
            for index, (arg_name, env_default_name) in enumerate(zip(typ.optargs, defaults)):
                default = self.append(ir.GetLocal(self.current_env, env_default_name))
                value = self.append(ir.Builtin("unwrap", [optargs[index], default],
                                               typ.optargs[arg_name]))
                self.append(ir.SetLocal(env, arg_name, value))

            result = self.visit(node.body)

            if is_lambda:
                self.terminate(ir.Return(result))
            elif builtins.is_none(typ.ret):
                if not self.current_block.is_terminated():
                    self.current_block.append(ir.Return(ir.Constant(None, builtins.TNone())))
            else:
                if not self.current_block.is_terminated():
                    self.current_block.append(ir.Unreachable())
        finally:
            self.name = old_name
            self.current_function = old_func
            self.current_block = old_block
            self.current_globals = old_globals
            self.current_env = old_env
            if not is_lambda:
                self.current_private_env = old_priv_env

        return self.append(ir.Closure(func, self.current_env))

    def visit_FunctionDefT(self, node):
        func = self.visit_function(node, is_lambda=False)
        self._set_local(node.name, func)

    def visit_Return(self, node):
        if node.value is None:
            return_value = ir.Constant(None, builtins.TNone())
        else:
            return_value = self.visit(node.value)

        if self.return_target is None:
            self.append(ir.Return(return_value))
        else:
            self.append(ir.SetLocal(self.current_private_env, ".return", return_value))
            self.append(ir.Branch(self.return_target))

    def visit_Expr(self, node):
        # ignore the value, do it for side effects
        self.visit(node.value)

    def visit_Assign(self, node):
        try:
            self.current_assign = self.visit(node.value)
            assert self.current_assign is not None
            for target in node.targets:
                self.visit(target)
        finally:
            self.current_assign = None

    def visit_AugAssign(self, node):
        lhs = self.visit(target)
        rhs = self.visit(node.value)
        value = self.append(ir.BinaryOp(node.op, lhs, rhs))
        try:
            self.current_assign = value
            self.visit(node.target)
        finally:
            self.current_assign = None

    def visit_If(self, node):
        cond = self.visit(node.test)
        head = self.current_block

        if_true = self.add_block()
        self.current_block = if_true
        self.visit(node.body)

        if any(node.orelse):
            if_false = self.add_block()
            self.current_block = if_false
            self.visit(node.orelse)

        tail = self.add_block()
        self.current_block = tail
        if not if_true.is_terminated():
            if_true.append(ir.Branch(tail))

        if any(node.orelse):
            if not if_false.is_terminated():
                if_false.append(ir.Branch(tail))
            head.append(ir.BranchIf(cond, if_true, if_false))
        else:
            head.append(ir.BranchIf(cond, if_true, tail))

    def visit_While(self, node):
        try:
            head = self.add_block("while.head")
            self.append(ir.Branch(head))
            self.current_block = head
            old_continue, self.continue_target = self.continue_target, head
            cond = self.visit(node.test)

            break_block = self.add_block("while.break")
            old_break, self.break_target = self.break_target, break_block

            body = self.add_block("while.body")
            self.current_block = body
            self.visit(node.body)

            if any(node.orelse):
                else_tail = self.add_block("while.else")
                self.current_block = else_tail
                self.visit(node.orelse)

            tail = self.add_block("while.tail")
            self.current_block = tail

            if any(node.orelse):
                if not else_tail.is_terminated():
                    else_tail.append(ir.Branch(tail))
            else:
                else_tail = tail

            head.append(ir.BranchIf(cond, body, else_tail))
            if not body.is_terminated():
                body.append(ir.Branch(head))
            break_block.append(ir.Branch(tail))
        finally:
            self.break_target = old_break
            self.continue_target = old_continue

    def _iterable_len(self, value, typ=builtins.TInt(types.TValue(32))):
        if builtins.is_list(value.type):
            return self.append(ir.Builtin("len", [value], typ))
        elif builtins.is_range(value.type):
            start  = self.append(ir.GetAttr(value, "start"))
            stop   = self.append(ir.GetAttr(value, "stop"))
            step   = self.append(ir.GetAttr(value, "step"))
            spread = self.append(ir.BinaryOp(ast.Sub(loc=None), stop, start))
            return self.append(ir.BinaryOp(ast.FloorDiv(loc=None), spread, step))
        else:
            assert False

    def _iterable_get(self, value, index):
        # Assuming the value is within bounds.
        if builtins.is_list(value.type):
            return self.append(ir.GetElem(value, index))
        elif builtins.is_range(value.type):
            start  = self.append(ir.GetAttr(value, "start"))
            step   = self.append(ir.GetAttr(value, "step"))
            offset = self.append(ir.BinaryOp(ast.Mult(loc=None), step, index))
            return self.append(ir.BinaryOp(ast.Add(loc=None), start, offset))
        else:
            assert False

    def visit_For(self, node):
        try:
            iterable = self.visit(node.iter)
            length = self._iterable_len(iterable)

            head = self.add_block("for.head")
            self.append(ir.Branch(head))
            self.current_block = head
            phi = self.append(ir.Phi(length.type))
            phi.add_incoming(ir.Constant(0, phi.type), head)
            cond = self.append(ir.Compare(ast.Lt(loc=None), phi, length))

            break_block = self.add_block("for.break")
            old_break, self.break_target = self.break_target, break_block

            continue_block = self.add_block("for.continue")
            old_continue, self.continue_target = self.continue_target, continue_block
            self.current_block = continue_block

            updated_index = self.append(ir.BinaryOp(ast.Add(loc=None), phi,
                                                    ir.Constant(1, phi.type)))
            phi.add_incoming(updated_index, continue_block)
            self.append(ir.Branch(head))

            body = self.add_block("for.body")
            self.current_block = body
            elt = self._iterable_get(iterable, phi)
            try:
                self.current_assign = elt
                self.visit(node.target)
            finally:
                self.current_assign = None
            self.visit(node.body)

            if any(node.orelse):
                else_tail = self.add_block("for.else")
                self.current_block = else_tail
                self.visit(node.orelse)

            tail = self.add_block("for.tail")
            self.current_block = tail

            if any(node.orelse):
                if not else_tail.is_terminated():
                    else_tail.append(ir.Branch(tail))
            else:
                else_tail = tail

            head.append(ir.BranchIf(cond, body, else_tail))
            if not body.is_terminated():
                body.append(ir.Branch(continue_block))
            break_block.append(ir.Branch(tail))
        finally:
            self.break_target = old_break
            self.continue_target = old_continue

    def visit_Break(self, node):
        self.append(ir.Branch(self.break_target))

    def visit_Continue(self, node):
        self.append(ir.Branch(self.continue_target))

    def visit_Raise(self, node):
        self.append(ir.Raise(self.visit(node.exc)))

    def visit_Try(self, node):
        dispatcher = self.add_block("try.dispatch")
        landingpad = dispatcher.append(ir.LandingPad())

        if any(node.finalbody):
            # k for continuation
            final_state = self.append(ir.Alloc([], ir.TEnvironment({ ".k": ir.TBasicBlock() })))
            final_targets = []

            if self.break_target is not None:
                break_proxy = self.add_block("try.break")
                old_break, self.break_target = self.break_target, break_proxy
                break_proxy.append(ir.SetLocal(final_state, ".k", old_break))
                final_targets.append(old_break)
            if self.continue_target is not None:
                continue_proxy = self.add_block("try.continue")
                old_continue, self.continue_target = self.continue_target, continue_proxy
                continue_proxy.append(ir.SetLocal(final_state, ".k", old_continue))
                final_targets.append(old_continue)

            return_proxy = self.add_block("try.return")
            old_return, self.return_target = self.return_target, return_proxy
            if old_return is not None:
                return_proxy.append(ir.SetLocal(final_state, ".k", old_return))
                final_targets.append(old_return)
            else:
                return_action = self.add_block("try.doreturn")
                value = return_action.append(ir.GetLocal(self.current_private_env, ".return"))
                return_action.append(ir.Return(value))
                return_proxy.append(ir.SetLocal(final_state, ".k", return_action))
                final_targets.append(return_action)

        body = self.add_block("try.body")
        self.append(ir.Branch(body))
        self.current_block = body

        try:
            old_unwind, self.unwind_target = self.unwind_target, dispatcher
            self.visit(node.body)
        finally:
            self.unwind_target = old_unwind

        self.visit(node.orelse)
        body = self.current_block

        if any(node.finalbody):
            if self.break_target:
                self.break_target = old_break
            if self.continue_target:
                self.continue_target = old_continue
            self.return_target = old_return

        handlers = []
        for handler_node in node.handlers:
            handler = self.add_block("handler." + handler_node.name_type.find().name)
            self.current_block = handler
            handlers.append(handler)
            landingpad.add_clause(handler, handler_node.name_type)

            if handler_node.name is not None:
                exn = self.append(ir.Builtin("exncast", [landingpad], handler_node.name_type))
                self._set_local(handler_node.name, exn)
            self.visit(handler_node.body)

        if any(node.finalbody):
            finalizer = self.add_block("finally")
            self.current_block = finalizer

            self.visit(node.finalbody)

            if not self.current_block.is_terminated():
                dest = self.append(ir.GetLocal(final_state, ".k"))
                self.append(ir.IndirectBranch(dest, final_targets))

        tail = self.add_block("try.tail")
        if any(node.finalbody):
            if self.break_target:
                break_proxy.append(ir.Branch(finalizer))
            if self.continue_target:
                continue_proxy.append(ir.Branch(finalizer))
            return_proxy.append(ir.Branch(finalizer))
        if not body.is_terminated():
            if any(node.finalbody):
                body.append(ir.SetLocal(final_state, ".k", tail))
                body.append(ir.Branch(finalizer))
                for handler in handlers:
                    if not handler.is_terminated():
                        handler.append(ir.SetLocal(final_state, ".k", tail))
                        handler.append(ir.Branch(tail))
            else:
                body.append(ir.Branch(tail))
                for handler in handlers:
                    if not handler.is_terminated():
                        handler.append(ir.Branch(tail))

        if any(tail.predecessors()):
            self.current_block = tail
        else:
            self.current_function.remove(tail)

    # TODO: With

    # Expression visitors
    # These visitors return a node in addition to mutating
    # the IR.

    def visit_LambdaT(self, node):
        return self.visit_function(node, is_lambda=True)

    def visit_IfExpT(self, node):
        cond = self.visit(node.test)
        head = self.current_block

        if_true = self.add_block()
        self.current_block = if_true
        true_result = self.visit(node.body)

        if_false = self.add_block()
        self.current_block = if_false
        false_result = self.visit(node.orelse)

        tail = self.add_block()
        self.current_block = tail

        if not if_true.is_terminated():
            if_true.append(ir.Branch(tail))
        if not if_false.is_terminated():
            if_false.append(ir.Branch(tail))
        head.append(ir.BranchIf(cond, if_true, if_false))

        phi = self.append(ir.Phi(node.type))
        phi.add_incoming(true_result, if_true)
        phi.add_incoming(false_result, if_false)
        return phi

    def visit_NumT(self, node):
        return ir.Constant(node.n, node.type)

    def visit_NameConstantT(self, node):
        return ir.Constant(node.value, node.type)

    def _env_for(self, name):
        if name in self.current_globals:
            return self.append(ir.Builtin("globalenv", [self.current_env],
                                          self.current_env.type.outermost()))
        else:
            return self.current_env

    def _get_local(self, name):
        return self.append(ir.GetLocal(self._env_for(name), name, name="local." + name))

    def _set_local(self, name, value):
        self.append(ir.SetLocal(self._env_for(name), name, value))

    def visit_NameT(self, node):
        if self.current_assign is None:
            return self._get_local(node.id)
        else:
            return self._set_local(node.id, self.current_assign)

    def visit_AttributeT(self, node):
        try:
            old_assign, self.current_assign = self.current_assign, None
            obj = self.visit(node.value)
        finally:
            self.current_assign = old_assign

        if self.current_assign is None:
            return self.append(ir.GetAttr(obj, node.attr,
                                          name="{}.{}".format(_readable_name(obj), node.attr)))
        else:
            self.append(ir.SetAttr(obj, node.attr, self.current_assign))

    def _map_index(self, length, index):
        lt_0          = self.append(ir.Compare(ast.Lt(loc=None),
                                               index, ir.Constant(0, index.type)))
        from_end      = self.append(ir.BinaryOp(ast.Add(loc=None), length, index))
        mapped_index  = self.append(ir.Select(lt_0, from_end, index))
        mapped_ge_0   = self.append(ir.Compare(ast.GtE(loc=None),
                                               mapped_index, ir.Constant(0, mapped_index.type)))
        mapped_lt_len = self.append(ir.Compare(ast.Lt(loc=None),
                                               mapped_index, length))
        in_bounds     = self.append(ir.Select(mapped_ge_0, mapped_lt_len,
                                              ir.Constant(False, builtins.TBool())))

        out_of_bounds_block = self.add_block()
        exn = out_of_bounds_block.append(ir.Alloc([], builtins.TIndexError()))
        out_of_bounds_block.append(ir.Raise(exn))

        in_bounds_block = self.add_block()

        self.append(ir.BranchIf(in_bounds, in_bounds_block, out_of_bounds_block))
        self.current_block = in_bounds_block

        return mapped_index

    def _make_check(self, cond, exn_gen):
        # cond:    bool Value, condition
        # exn_gen: lambda()->exn Value, exception if condition not true
        cond_block = self.current_block

        self.current_block = body_block = self.add_block()
        self.append(ir.Raise(exn_gen()))

        self.current_block = tail_block = self.add_block()
        cond_block.append(ir.BranchIf(cond, tail_block, body_block))

    def _make_loop(self, init, cond_gen, body_gen):
        # init:     'iter Value, initial loop variable value
        # cond_gen: lambda('iter Value)->bool Value, loop condition
        # body_gen: lambda('iter Value)->'iter Value, loop body,
        #               returns next loop variable value
        init_block = self.current_block

        self.current_block = head_block = self.add_block()
        init_block.append(ir.Branch(head_block))
        phi = self.append(ir.Phi(init.type))
        phi.add_incoming(init, init_block)
        cond = cond_gen(phi)

        self.current_block = body_block = self.add_block()
        body = body_gen(phi)
        self.append(ir.Branch(head_block))
        phi.add_incoming(body, self.current_block)

        self.current_block = tail_block = self.add_block()
        head_block.append(ir.BranchIf(cond, body_block, tail_block))

        return head_block, body_block, tail_block

    def visit_SubscriptT(self, node):
        try:
            old_assign, self.current_assign = self.current_assign, None
            value = self.visit(node.value)
        finally:
            self.current_assign = old_assign

        if isinstance(node.slice, ast.Index):
            index = self.visit(node.slice.value)
            length = self._iterable_len(value, index.type)
            mapped_index = self._map_index(length, index)
            if self.current_assign is None:
                result = self._iterable_get(value, mapped_index)
                result.set_name("{}.at.{}".format(value.name, _readable_name(index)))
                return result
            else:
                self.append(ir.SetElem(value, mapped_index, self.current_assign,
                                       name="{}.at.{}".format(value.name, _readable_name(index))))
        else: # Slice
            length = self._iterable_len(value, node.slice.type)

            if node.slice.lower is not None:
                min_index = self.visit(node.slice.lower)
            else:
                min_index = ir.Constant(0, node.slice.type)
            mapped_min_index = self._map_index(length, min_index)

            if node.slice.upper is not None:
                max_index = self.visit(node.slice.upper)
            else:
                max_index = length
            mapped_max_index = self._map_index(length, max_index)

            if node.slice.step is not None:
                step = self.visit(node.slice.step)
            else:
                step = ir.Constant(1, node.slice.type)

            unstepped_size = self.append(ir.BinaryOp(ast.Sub(loc=None),
                                                     mapped_max_index, mapped_min_index))
            slice_size = self.append(ir.BinaryOp(ast.FloorDiv(loc=None), unstepped_size, step))

            self._make_check(self.append(ir.Compare(ast.Eq(loc=None), slice_size, length)),
                             lambda: self.append(ir.Alloc([], builtins.TValueError())))

            if self.current_assign is None:
                other_value = self.append(ir.Alloc([slice_size], value.type))
            else:
                other_value = self.current_assign

            def body_gen(other_index):
                offset = self.append(ir.BinaryOp(ast.Mult(loc=None), step, other_index))
                index = self.append(ir.BinaryOp(ast.Add(loc=None), min_index, offset))

                if self.current_assign is None:
                    elem = self._iterable_get(value, index)
                    self.append(ir.SetElem(other_value, other_index, elem))
                else:
                    elem = self.append(ir.GetElem(self.current_assign, other_index))
                    self.append(ir.SetElem(value, index, elem))

                return self.append(ir.BinaryOp(ast.Add(loc=None), other_index,
                                               ir.Constant(1, node.slice.type)))
            self._make_loop(ir.Constant(0, node.slice.type),
                lambda index: self.append(ir.Compare(ast.Lt(loc=None), index, slice_size)),
                body_gen)

    def visit_TupleT(self, node):
        if self.current_assign is None:
            return self.append(ir.Alloc([self.visit(elt) for elt in node.elts], node.type))
        else:
            try:
                old_assign = self.current_assign
                for index, elt_node in enumerate(node.elts):
                    self.current_assign = \
                        self.append(ir.GetAttr(old_assign, index,
                                               name="{}.{}".format(old_assign.name, index)),
                                    loc=elt_node.loc)
                    self.visit(elt_node)
            finally:
                self.current_assign = old_assign

    def visit_ListT(self, node):
        if self.current_assign is None:
            elts = [self.visit(elt_node) for elt_node in node.elts]
            lst = self.append(ir.Alloc([ir.Constant(len(node.elts), self._size_type)],
                                       node.type))
            for index, elt_node in enumerate(elts):
                self.append(ir.SetElem(lst, ir.Constant(index, self._size_type), elt_node))
            return lst
        else:
            length = self.append(ir.Builtin("len", [self.current_assign], self._size_type))
            self._make_check(self.append(ir.Compare(ast.Eq(loc=None), length,
                                                    ir.Constant(len(node.elts), self._size_type))),
                             lambda: self.append(ir.Alloc([], builtins.TValueError())))

            for index, elt_node in enumerate(node.elts):
                elt = self.append(ir.GetElem(self.current_assign,
                                             ir.Constant(index, self._size_type)))
                try:
                    old_assign, self.current_assign = self.current_assign, elt
                    self.visit(elt_node)
                finally:
                    self.current_assign = old_assign

    def visit_ListCompT(self, node):
        assert len(node.generators) == 1
        comprehension = node.generators[0]
        assert comprehension.ifs == []

        iterable = self.visit(comprehension.iter)
        length = self._iterable_len(iterable)
        result = self.append(ir.Alloc([length], node.type))

        try:
            env_type = ir.TEnvironment(node.typing_env, self.current_env.type)
            env = self.append(ir.Alloc([], env_type, name="env.gen"))
            old_env, self.current_env = self.current_env, env

            self.append(ir.SetLocal(env, ".outer", old_env))

            def body_gen(index):
                elt = self._iterable_get(iterable, index)
                try:
                    old_assign, self.current_assign = self.current_assign, elt
                    print(comprehension.target, self.current_assign)
                    self.visit(comprehension.target)
                finally:
                    self.current_assign = old_assign

                mapped_elt = self.visit(node.elt)
                self.append(ir.SetElem(result, index, mapped_elt))
                return self.append(ir.BinaryOp(ast.Add(loc=None), index,
                                               ir.Constant(1, length.type)))
            self._make_loop(ir.Constant(0, length.type),
                lambda index: self.append(ir.Compare(ast.Lt(loc=None), index, length)),
                body_gen)

            return result
        finally:
            self.current_env = old_env

    def visit_BoolOpT(self, node):
        blocks = []
        for value_node in node.values:
            value = self.visit(value_node)
            blocks.append((value, self.current_block))
            self.current_block = self.add_block()

        tail = self.current_block
        phi = self.append(ir.Phi(node.type))
        for ((value, block), next_block) in zip(blocks, [b for (v,b) in blocks[1:]] + [tail]):
            phi.add_incoming(value, block)
            if isinstance(node.op, ast.And):
                block.append(ir.BranchIf(value, next_block, tail))
            else:
                block.append(ir.BranchIf(value, tail, next_block))
        return phi

    def visit_UnaryOpT(self, node):
        if isinstance(node.op, ast.Not):
            return self.append(ir.Select(node.operand,
                        ir.Constant(False, builtins.TBool()),
                        ir.Constant(True,  builtins.TBool())))
        else: # Numeric operators
            return self.append(ir.UnaryOp(node.op, self.visit(node.operand)))

    def visit_CoerceT(self, node):
        value = self.visit(node.value)
        if node.type.find() == value.type:
            return value
        else:
            return self.append(ir.Coerce(value, node.type,
                                         name="{}.{}".format(_readable_name(value),
                                                             node.type.name)))

    def visit_BinOpT(self, node):
        if builtins.is_numeric(node.type):
            return self.append(ir.BinaryOp(node.op,
                        self.visit(node.left),
                        self.visit(node.right)))
        elif isinstance(node.op, ast.Add): # list + list, tuple + tuple
            lhs, rhs = self.visit(node.left), self.visit(node.right)
            if types.is_tuple(node.left.type) and builtins.is_tuple(node.right.type):
                elts = []
                for index, elt in enumerate(node.left.type.elts):
                    elts.append(self.append(ir.GetAttr(lhs, index)))
                for index, elt in enumerate(node.right.type.elts):
                    elts.append(self.append(ir.GetAttr(rhs, index)))
                return self.append(ir.Alloc(elts, node.type))
            elif builtins.is_list(node.left.type) and builtins.is_list(node.right.type):
                lhs_length = self.append(ir.Builtin("len", [lhs], self._size_type))
                rhs_length = self.append(ir.Builtin("len", [rhs], self._size_type))

                result_length = self.append(ir.BinaryOp(ast.Add(loc=None), lhs_length, rhs_length))
                result = self.append(ir.Alloc([result_length], node.type))

                # Copy lhs
                def body_gen(index):
                    elt = self.append(ir.GetElem(lhs, index))
                    self.append(ir.SetElem(result, index, elt))
                    return self.append(ir.BinaryOp(ast.Add(loc=None), index,
                                                   ir.Constant(1, self._size_type)))
                self._make_loop(ir.Constant(0, self._size_type),
                    lambda index: self.append(ir.Compare(ast.Lt(loc=None), index, lhs_length)),
                    body_gen)

                # Copy rhs
                def body_gen(index):
                    elt = self.append(ir.GetElem(rhs, index))
                    result_index = self.append(ir.BinaryOp(ast.Add(loc=None), index, lhs_length))
                    self.append(ir.SetElem(result, result_index, elt))
                    return self.append(ir.BinaryOp(ast.Add(loc=None), index,
                                                   ir.Constant(1, self._size_type)))
                self._make_loop(ir.Constant(0, self._size_type),
                    lambda index: self.append(ir.Compare(ast.Lt(loc=None), index, rhs_length)),
                    body_gen)

                return result
            else:
                assert False
        elif isinstance(node.op, ast.Mult): # list * int, int * list
            lhs, rhs = self.visit(node.left), self.visit(node.right)
            if builtins.is_list(lhs.type) and builtins.is_int(rhs.type):
                lst, num = lhs, rhs
            elif builtins.is_int(lhs.type) and builtins.is_list(rhs.type):
                lst, num = rhs, lhs
            else:
                assert False

            lst_length = self.append(ir.Builtin("len", [lst], self._size_type))

            result_length = self.append(ir.BinaryOp(ast.Mult(loc=None), lst_length, num))
            result = self.append(ir.Alloc([result_length], node.type))

            # num times...
            def body_gen(num_index):
                # ... copy the list
                def body_gen(lst_index):
                    elt = self.append(ir.GetElem(lst, lst_index))
                    base_index = self.append(ir.BinaryOp(ast.Mult(loc=None),
                                                         num_index, lst_length))
                    result_index = self.append(ir.BinaryOp(ast.Add(loc=None),
                                                           base_index, lst_index))
                    self.append(ir.SetElem(result, base_index, elt))
                    return self.append(ir.BinaryOp(ast.Add(loc=None), lst_index,
                                                   ir.Constant(1, self._size_type)))
                self._make_loop(ir.Constant(0, self._size_type),
                    lambda index: self.append(ir.Compare(ast.Lt(loc=None), index, lst_length)),
                    body_gen)

                return self.append(ir.BinaryOp(ast.Add(loc=None), lst_length,
                                               ir.Constant(1, self._size_type)))
            self._make_loop(ir.Constant(0, self._size_type),
                lambda index: self.append(ir.Compare(ast.Lt(loc=None), index, num)),
                body_gen)
        else:
            assert False

    def _compare_pair_order(self, op, lhs, rhs):
        if builtins.is_numeric(lhs.type) and builtins.is_numeric(rhs.type):
            return self.append(ir.Compare(op, lhs, rhs))
        elif types.is_tuple(lhs.type) and types.is_tuple(rhs.type):
            result = None
            for index in range(len(lhs.type.elts)):
                lhs_elt = self.append(ir.GetAttr(lhs, index))
                rhs_elt = self.append(ir.GetAttr(rhs, index))
                elt_result = self.append(ir.Compare(op, lhs_elt, rhs_elt))
                if result is None:
                    result = elt_result
                else:
                    result = self.append(ir.Select(result, elt_result,
                                                   ir.Constant(False, builtins.TBool())))
            return result
        elif builtins.is_list(lhs.type) and builtins.is_list(rhs.type):
            head = self.current_block
            lhs_length = self.append(ir.Builtin("len", [lhs], self._size_type))
            rhs_length = self.append(ir.Builtin("len", [rhs], self._size_type))
            compare_length = self.append(ir.Compare(op, lhs_length, rhs_length))
            eq_length = self.append(ir.Compare(ast.Eq(loc=None), lhs_length, rhs_length))

            # If the length is the same, compare element-by-element
            # and break when the comparison result is false
            loop_head = self.add_block()
            self.current_block = loop_head
            index_phi = self.append(ir.Phi(self._size_type))
            index_phi.add_incoming(ir.Constant(0, self._size_type), head)
            loop_cond = self.append(ir.Compare(ast.Lt(loc=None), index_phi, lhs_length))

            loop_body = self.add_block()
            self.current_block = loop_body
            lhs_elt = self.append(ir.GetElem(lhs, index_phi))
            rhs_elt = self.append(ir.GetElem(rhs, index_phi))
            body_result = self._compare_pair(op, lhs_elt, rhs_elt)

            loop_body2 = self.add_block()
            self.current_block = loop_body2
            index_next = self.append(ir.BinaryOp(ast.Add(loc=None), index_phi,
                                                 ir.Constant(1, self._size_type)))
            self.append(ir.Branch(loop_head))
            index_phi.add_incoming(index_next, loop_body2)

            tail = self.add_block()
            self.current_block = tail
            phi = self.append(ir.Phi(builtins.TBool()))
            head.append(ir.BranchIf(eq_length, loop_head, tail))
            phi.add_incoming(compare_length, head)
            loop_head.append(ir.BranchIf(loop_cond, loop_body, tail))
            phi.add_incoming(ir.Constant(True, builtins.TBool()), loop_head)
            loop_body.append(ir.BranchIf(body_result, loop_body2, tail))
            phi.add_incoming(body_result, loop_body)

            if isinstance(op, ast.NotEq):
                result = self.append(ir.Select(phi,
                    ir.Constant(False, builtins.TBool()), ir.Constant(True, builtins.TBool())))
            else:
                result = phi

            return result
        else:
            assert False

    def _compare_pair_inclusion(self, op, needle, haystack):
        if builtins.is_range(haystack.type):
            # Optimized range `in` operator
            start       = self.append(ir.GetAttr(haystack, "start"))
            stop        = self.append(ir.GetAttr(haystack, "stop"))
            step        = self.append(ir.GetAttr(haystack, "step"))
            after_start = self.append(ir.Compare(ast.GtE(loc=None), needle, start))
            after_stop  = self.append(ir.Compare(ast.Lt(loc=None), needle, stop))
            from_start  = self.append(ir.BinaryOp(ast.Sub(loc=None), needle, start))
            mod_step    = self.append(ir.BinaryOp(ast.Mod(loc=None), from_start, step))
            on_step     = self.append(ir.Compare(ast.Eq(loc=None), mod_step,
                                                 ir.Constant(0, mod_step.type)))
            result      = self.append(ir.Select(after_start, after_stop,
                                                ir.Constant(False, builtins.TBool())))
            result      = self.append(ir.Select(result, on_step,
                                                ir.Constant(False, builtins.TBool())))
        elif builtins.is_iterable(haystack.type):
            length = self._iterable_len(haystack)

            cmp_result = loop_body2 = None
            def body_gen(index):
                nonlocal cmp_result, loop_body2

                elt = self._iterable_get(haystack, index)
                cmp_result = self._compare_pair(ast.Eq(loc=None), needle, elt)

                loop_body2 = self.add_block()
                self.current_block = loop_body2
                return self.append(ir.BinaryOp(ast.Add(loc=None), index,
                                               ir.Constant(1, length.type)))
            loop_head, loop_body, loop_tail = \
                self._make_loop(ir.Constant(0, length.type),
                    lambda index: self.append(ir.Compare(ast.Lt(loc=None), index, length)),
                    body_gen)

            loop_body.append(ir.BranchIf(cmp_result, loop_tail, loop_body2))
            phi = loop_tail.prepend(ir.Phi(builtins.TBool()))
            phi.add_incoming(ir.Constant(False, builtins.TBool()), loop_head)
            phi.add_incoming(ir.Constant(True, builtins.TBool()), loop_body)

            result = phi
        else:
            assert False

        if isinstance(op, ast.NotIn):
            result = self.append(ir.Select(result,
                        ir.Constant(False, builtins.TBool()),
                        ir.Constant(True, builtins.TBool())))

        return result

    def _compare_pair_identity(self, op, lhs, rhs):
        if builtins.is_mutable(lhs) and builtins.is_mutable(rhs):
            # These are actually pointers, compare directly.
            return self.append(ir.Compare(op, lhs, rhs))
        else:
            # Compare by value instead, our backend cannot handle
            # equality of aggregates.
            if isinstance(op, ast.Is):
                op = ast.Eq(loc=None)
            elif isinstance(op, ast.IsNot):
                op = ast.NotEq(loc=None)
            else:
                assert False
            return self._compare_pair_order(op, lhs, rhs)

    def _compare_pair(self, op, lhs, rhs):
        if isinstance(op, (ast.Is, ast.IsNot)):
            return self._compare_pair_identity(op, lhs, rhs)
        elif isinstance(op, (ast.In, ast.NotIn)):
            return self._compare_pair_inclusion(op, lhs, rhs)
        else: # Eq, NotEq, Lt, LtE, Gt, GtE
            return self._compare_pair_order(op, lhs, rhs)

    def visit_CompareT(self, node):
        # Essentially a sequence of `and`s performed over results
        # of comparisons.
        blocks = []
        lhs = self.visit(node.left)
        for op, rhs_node in zip(node.ops, node.comparators):
            rhs = self.visit(rhs_node)
            result = self._compare_pair(op, lhs, rhs)
            blocks.append((result, self.current_block))
            self.current_block = self.add_block()
            lhs = rhs

        tail = self.current_block
        phi = self.append(ir.Phi(node.type))
        for ((value, block), next_block) in zip(blocks, [b for (v,b) in blocks[1:]] + [tail]):
            phi.add_incoming(value, block)
            block.append(ir.BranchIf(value, next_block, tail))
        return phi

    def visit_builtin_call(self, node):
        # A builtin by any other name... Ignore node.func, just use the type.
        typ = node.func.type
        if types.is_builtin(typ, "bool"):
            if len(node.args) == 0 and len(node.keywords) == 0:
                return ir.Constant(False, builtins.TBool())
            elif len(node.args) == 1 and len(node.keywords) == 0:
                arg = self.visit(node.args[0])
                return self.append(ir.Select(arg,
                    ir.Constant(True,  builtins.TBool()),
                    ir.Constant(False, builtins.TBool())))
            else:
                assert False
        elif types.is_builtin(typ, "int"):
            if len(node.args) == 0 and len(node.keywords) == 0:
                return ir.Constant(0, node.type)
            elif len(node.args) == 1 and \
                    (len(node.keywords) == 0 or \
                     len(node.keywords) == 1 and node.keywords[0].arg == 'width'):
                # The width argument is purely type-level
                arg = self.visit(node.args[0])
                return self.append(ir.Coerce(arg, node.type))
            else:
                assert False
        elif types.is_builtin(typ, "float"):
            if len(node.args) == 0 and len(node.keywords) == 0:
                return ir.Constant(0.0, builtins.TFloat())
            elif len(node.args) == 1 and len(node.keywords) == 0:
                arg = self.visit(node.args[0])
                return self.append(ir.Coerce(arg, node.type))
            else:
                assert False
        elif types.is_builtin(typ, "list"):
            if len(node.args) == 0 and len(node.keywords) == 0:
                length = ir.Constant(0, builtins.TInt(types.TValue(32)))
                return self.append(ir.Alloc(node.type, length))
            elif len(node.args) == 1 and len(node.keywords) == 0:
                arg = self.visit(node.args[0])
                length = self._iterable_len(arg)
                result = self.append(ir.Alloc([length], node.type))

                def body_gen(index):
                    elt = self._iterable_get(arg, index)
                    self.append(ir.SetElem(result, index, elt))
                    return self.append(ir.BinaryOp(ast.Add(loc=None), index,
                                                   ir.Constant(1, length.type)))
                self._make_loop(ir.Constant(0, length.type),
                    lambda index: self.append(ir.Compare(ast.Lt(loc=None), index, length)),
                    body_gen)

                return result
            else:
                assert False
        elif types.is_builtin(typ, "range"):
            elt_typ = builtins.get_iterable_elt(node.type)
            if len(node.args) == 1 and len(node.keywords) == 0:
                max_arg = self.visit(node.args[0])
                return self.append(ir.Alloc([
                    ir.Constant(elt_typ.zero(), elt_typ),
                    max_arg,
                    ir.Constant(elt_typ.one(), elt_typ),
                ], node.type))
            elif len(node.args) == 2 and len(node.keywords) == 0:
                min_arg = self.visit(node.args[0])
                max_arg = self.visit(node.args[1])
                return self.append(ir.Alloc([
                    min_arg,
                    max_arg,
                    ir.Constant(elt_typ.one(), elt_typ),
                ], node.type))
            elif len(node.args) == 3 and len(node.keywords) == 0:
                min_arg = self.visit(node.args[0])
                max_arg = self.visit(node.args[1])
                step_arg = self.visit(node.args[2])
                return self.append(ir.Alloc([
                    min_arg,
                    max_arg,
                    step_arg,
                ], node.type))
            else:
                assert False
        elif types.is_builtin(typ, "len"):
            if len(node.args) == 1 and len(node.keywords) == 0:
                arg = self.visit(node.args[0])
                return self._iterable_len(arg)
            else:
                assert False
        elif types.is_builtin(typ, "round"):
            if len(node.args) == 1 and len(node.keywords) == 0:
                arg = self.visit(node.args[0])
                return self.append(ir.Builtin("round", [arg]))
            else:
                assert False
        elif types.is_exn_constructor(typ):
            return self.append(ir.Alloc([self.visit(arg) for args in node.args], node.type))
        else:
            assert False

    def visit_CallT(self, node):
        if types.is_builtin(node.func.type):
            return self.visit_builtin_call(node)
        else:
            typ = node.func.type.find()
            func = self.visit(node.func)
            args = [self.visit(arg) for arg in node.args]
            for index, optarg_name in enumerate(typ.optargs):
                if len(typ.args) + index >= len(args):
                    optarg_typ = ir.TOption(typ.optargs[optarg_name])
                    for keyword in node.keywords:
                        if keyword.arg == optarg_name:
                            value = self.append(ir.Alloc(optarg_typ, [self.visit(keyword.value)]))
                            args.append(value)
                            break
                    else:
                        value = self.append(ir.Alloc(optarg_typ, []))
                        args.append(value)

            if self.unwind_target is None:
                return self.append(ir.Call(func, args))
            else:
                after_invoke = self.add_block()
                invoke = self.append(ir.Invoke(func, args, after_invoke, self.unwind_target))
                self.current_block = after_invoke
                return invoke
