import numpy as np
import jax
import jax.numpy as jnp
import jax.random as random
import optax
from typing import Dict, Generator
import warnings

from pyRDDLGym.Core.ErrorHandling.RDDLException import RDDLNotImplementedError
from pyRDDLGym.Core.Jax.JaxRDDLCompiler import JaxRDDLCompiler
from pyRDDLGym.Core.Parser.rddl import RDDL


class FuzzyLogic:
    
    def __init__(self, soft_if: bool=True):
        self.soft_if = soft_if
        
    def _and(self, a, b):
        raise NotImplementedError
    
    def _not(self, x):
        return 1.0 - x
    
    def _or(self, a, b):
        return self._not(self._and(self._not(a), self._not(b)))
    
    def _xor(self, a, b):
        return self._and(self._or(a, b), self._not(self._and(a, b)))
    
    def _implies(self, a, b):
        return self._or(self._not(a), b)
    
    def _forall(self, x, axis=None):
        raise NotImplementedError
    
    def _exists(self, x, axis=None):
        return self._not(self._forall(self._not(x), axis=axis))
    
    def _if_then_else(self, p, a, b):
        if self.soft_if:
            return p * a + (1.0 - p) * b
        else:
            return jnp.where(p, a, b)


class ProductLogic(FuzzyLogic):
    
    def _and(self, a, b):
        return a * b

    def _or(self, a, b):
        return a + b - a * b
    
    def _implies(self, a, b):
        return 1.0 - a * (1.0 - b)

    def _forall(self, x, axis=None):
        return jnp.prod(x, axis=axis)
    

class MinimumLogic(FuzzyLogic):
    
    def _and(self, a, b):
        return jnp.minimum(a, b)
    
    def _or(self, a, b):
        return jnp.maximum(a, b)
    
    def _forall(self, x, axis=None):
        return jnp.min(x, axis=axis)
    
    def _exists(self, x, axis=None):
        return jnp.max(x, axis=axis)
    

class JaxRDDLBackpropCompiler(JaxRDDLCompiler):
    
    def __init__(self, rddl: RDDL, logic: FuzzyLogic) -> None:
        super(JaxRDDLBackpropCompiler, self).__init__(rddl, allow_discrete=False)
        
        self.LOGICAL_OPS = {
            '^': logic._and,
            '|': logic._or,
            '~': logic._xor,
            '=>': logic._implies,
            '<=>': jnp.equal
        }
        self.LOGICAL_NOT = logic._not
        self.AGGREGATION_OPS = {
            'sum': jnp.sum,
            'avg': jnp.mean,
            'prod': jnp.prod,
            'min': jnp.min,
            'max': jnp.max,
            'forall': logic._forall,
            'exists': logic._exists
        }
        self.CONTROL_OPS = {
            'if': logic._if_then_else
        }
        
    def _jax_logical(self, expr, op, params):
        warnings.warn('Logical operator {} will be converted to fuzzy variant.'.format(op),
                      FutureWarning, stacklevel=2)
        
        return super(JaxRDDLBackpropCompiler, self)._jax_logical(expr, op, params)
    
    def _jax_aggregation(self, expr, op, params):
        warnings.warn('Aggregation operator {} will be converted to fuzzy variant.'.format(op),
                      FutureWarning, stacklevel=2)
        
        return super(JaxRDDLBackpropCompiler, self)._jax_aggregation(expr, op, params)
        
    def _jax_control(self, expr, op, params):
        warnings.warn('If statement will be converted to fuzzy variant.',
                      FutureWarning, stacklevel=2)
        
        return super(JaxRDDLBackpropCompiler, self)._jax_control(expr, op, params)

    def _jax_kron(self, expr, params):
        warnings.warn('KronDelta will be ignored.', FutureWarning, stacklevel=2)            
        arg, = expr.args
        return self._jax(arg, params)
    
    def _jax_poisson(self, expr, params):
        raise RDDLNotImplementedError(
            'No parameterization implemented for Poisson.' + '\n' + 
            JaxRDDLBackpropCompiler._print_stack_trace(expr))
    
    def _jax_gamma(self, expr, params):
        raise RDDLNotImplementedError(
            'No parameterization implemented for Gamma.' + '\n' + 
            JaxRDDLBackpropCompiler._print_stack_trace(expr))

 
class JaxRDDLBackpropPlanner:
    
    def __init__(self,
                 rddl: RDDL,
                 key: jax.random.PRNGKey,
                 n_batch: int,
                 action_bounds: Dict={},
                 initializer: jax.nn.initializers.Initializer=jax.nn.initializers.zeros,
                 optimizer: optax.GradientTransformation=optax.rmsprop(0.01),
                 logic: FuzzyLogic=ProductLogic()) -> None:
        self.key = key
        self.n_batch = n_batch
        
        compiled = JaxRDDLBackpropCompiler(rddl, logic=logic).compile()
        test_compiled = JaxRDDLCompiler(rddl).compile()
        
        self.finite_action_bounds = {}
        for name, (lb, ub) in action_bounds.items():
            if np.isfinite(lb) and np.isfinite(ub) and lb <= ub:
                self.finite_action_bounds[name] = (lb, ub)
                
        self.action_info = {}
        for pvar in rddl.domain.action_fluents.values():
            name = pvar.name       
            pvar_type = JaxRDDLCompiler.RDDL_TO_JAX_TYPE[pvar.range]
            value = compiled.init_values[name]
            shape = (compiled.horizon,)
            if hasattr(value, 'shape'):
                shape = shape + value.shape
            self.action_info[name] = (pvar_type, shape)
        
        predict_map, test_map = self._jax_predict()
        self.test_map = jax.jit(test_map)
        
        train_rollout = self._jax_rollout(compiled)
        train_loss = self._jax_evaluate(predict_map, train_rollout)
        self.train_loss = jax.jit(train_loss)
        
        test_rollout = self._jax_rollout(test_compiled)
        test_loss = self._jax_evaluate(test_map, test_rollout)
        self.test_loss = jax.jit(test_loss)
        
        self.initialize = jax.jit(self._jax_init(initializer, optimizer))
        self.update = jax.jit(self._jax_update(train_loss, optimizer))
    
    def _jax_predict(self):
        action_info = self.action_info
        action_bounds = self.finite_action_bounds
        
        def _predict(params):
            plan = {}
            for name, param in params.items():
                atype, _ = action_info[name]                
                if atype == bool:
                    action = jax.nn.sigmoid(param)
                elif atype == JaxRDDLCompiler.INT:
                    action = jnp.asarray(param, dtype=JaxRDDLCompiler.REAL)
                else:
                    action = param                
                if atype != bool and name in action_bounds:
                    lb, ub = action_bounds[name]
                    action = lb + (ub - lb) * jax.nn.sigmoid(action)                    
                plan[name] = action                
            return plan
    
        def _predict_test(params):
            plan = _predict(params)
            new_plan = {}
            for action, value in plan.items():
                action_type, _ = action_info[action]
                if action_type == bool:
                    new_plan[action] = jnp.greater(value, 0.5)
                elif action_type == JaxRDDLCompiler.INT:
                    new_plan[action] = jnp.asarray(jnp.round(value), dtype=JaxRDDLCompiler.INT)
                else:
                    new_plan[action] = value
            return new_plan
        
        return _predict, _predict_test
    
    def _jax_rollout(self, compiled):
        NORMAL = JaxRDDLCompiler.ERROR_CODES['NORMAL']
        n_batch = self.n_batch
        
        init_values = compiled.init_values
        primed_unprimed = compiled.state_unprimed
        _, cpfs = compiled.cpfs
        _, reward_fn = compiled.reward
        gamma = compiled.discount                
        
        def _epoch(carry, action):
            x, discount, key, err = carry            
            x.update(action)
            
            for name, cpf_fn in cpfs.items():
                x[name], key, cpf_err = cpf_fn(x, key)
                err |= cpf_err
            reward, key, reward_err = reward_fn(x, key)
            err |= reward_err
            
            reward = reward * discount
            discount *= gamma
            
            for primed, unprimed in primed_unprimed.items():
                x[unprimed] = x[primed]
            
            carry = (x, discount, key, err)
            return carry, reward
        
        def _rollout(plan, x0, key): 
            x0 = x0.copy()
            for primed, unprimed in primed_unprimed.items():
                x0[primed] = x0[unprimed]
            
            initial = (x0, 1.0, key, NORMAL)
            final, rewards = jax.lax.scan(_epoch, initial, plan)
            x, _, key, err = final
            cuml_reward = jnp.sum(rewards)
            
            return cuml_reward, x, key, err
        
        def _batched(value):
            value = jnp.asarray(value)
            batched_shape = (n_batch,) + value.shape
            batched_value = jnp.broadcast_to(value, shape=batched_shape) 
            return batched_value
                
        def _rollouts(plan, key):
            batched_init = jax.tree_map(_batched, init_values)
            keys = jax.random.split(key, num=n_batch)
            returns, batch, keys, errs = jax.vmap(_rollout, in_axes=(None, 0, 0))(
                plan, batched_init, keys)
            key = keys[-1]
            return returns, key, batch, errs
        
        return _rollouts
         
    def _jax_evaluate(self, predict, batched_rollouts):
        NORMAL = JaxRDDLCompiler.ERROR_CODES['NORMAL']
        
        def _loss(params, key):
            plan = predict(params)
            returns, key, batch, errs = batched_rollouts(plan, key)
            return_value = -jnp.mean(returns)
            errs = jax.lax.reduce(errs, NORMAL, jnp.bitwise_or, (0,))
            aux = (key, batch, errs)
            return return_value, aux
        
        return _loss
    
    def _jax_init(self, initializer, optimizer):
        action_info = self.action_info
        
        def _init(key):
            params = {}
            for action, (_, shape) in action_info.items():
                key, subkey = random.split(key)
                params[action] = initializer(subkey, shape, dtype=JaxRDDLCompiler.REAL)
            opt_state = optimizer.init(params)
            return params, opt_state, key
        
        return _init
    
    def _jax_update(self, loss, optimizer):
        
        def _update(params, opt_state, key):
            grad, (key, batch, errs) = jax.grad(loss, has_aux=True)(params, key)
            updates, opt_state = optimizer.update(grad, opt_state)
            params = optax.apply_updates(params, updates)       
            return params, opt_state, key, batch, errs
        
        return _update
            
    def optimize(self, n_epochs: int) -> Generator[Dict, None, None]:
        ''' Compute an optimal straight-line plan for the RDDL domain and instance.
        
        @param n_epochs: the maximum number of steps of gradient descent
        '''
        params, opt_state, key = self.initialize(self.key)
        
        best_params = params
        best_plan = self.test_map(best_params)
        best_loss = float('inf')
        
        for step in range(n_epochs):
            params, opt_state, key, *_ = self.update(params, opt_state, key)  
            
            train_loss, (key, *_) = self.train_loss(params, key)
            
            test_loss, (key, rollouts, errors) = self.test_loss(params, key)
            test_errors = JaxRDDLCompiler.get_error_codes(errors)
            
            if test_loss < best_loss:
                best_params = params
                best_plan = self.test_map(params)
                best_loss = test_loss
            
            callback = {'step': step,
                        'train_loss': train_loss,
                        'test_loss': test_loss,
                        'test_errors': test_errors,
                        'best_plan': best_plan,
                        'best_loss': best_loss,
                        'rollouts': rollouts}
            self.key = key
            yield callback
    